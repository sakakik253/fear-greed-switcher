#!/usr/bin/env python3
"""Fear & Greed 切替戦略のバックテスト（fear-greed-switcher）

戦略（動画で Ran Neuner 氏が説明したルール）:
  極端な恐怖 (Extreme Fear) に入った日  → BTC 現物を売って MSTR を買う
  極端な強欲 (Extreme Greed) に入った日 → MSTR を売って BTC 現物に戻す
  それ以外の日                         → 何もしない（常にどちらかを 100% 保有）

前提:
  - 指数は毎日 00:00 UTC（日本時間 9:00）に公表される。その日の終値で切替を実行する。
  - MSTR は米国市場の営業日にしか売買できないので、休場日のシグナルは翌営業日の終値で実行する。
  - 休場日の MSTR 評価額は直前の終値で据え置く（週末のリターンは 0）。
  - 開始日の初期ポジションは「開始日より前の最後の極端シグナル」から決める（ウォームスタート）。

使い方:
  python3 backtest.py [--start 2021-09-04] [--end 2026-09-04] [--cost-bps 30] [--out 出力先]
  データは同じフォルダ階層の data/fng.json と data/prices.json を読む。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BTC, MSTR = "BTC", "MSTR"
EXTREME_FEAR, EXTREME_GREED = "Extreme Fear", "Extreme Greed"


# ---------------------------------------------------------------------------
# データ読み込み
# ---------------------------------------------------------------------------
def load_frame(data_dir: Path) -> pd.DataFrame:
    """指数と価格を日次カレンダーに揃えた DataFrame を返す。"""
    fng_raw = json.loads((data_dir / "fng.json").read_text(encoding="utf-8"))["data"]
    prices_raw = json.loads((data_dir / "prices.json").read_text(encoding="utf-8"))

    fng = pd.DataFrame(fng_raw)
    fng["date"] = pd.to_datetime(fng["date"])
    fng = fng.set_index("date").sort_index()

    def series(key: str) -> pd.Series:
        frame = pd.DataFrame(prices_raw[key]["data"])
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date")["close"].sort_index().astype(float)

    btc, mstr = series("btc"), series("mstr")
    last_day = min(fng.index.max(), btc.index.max(), mstr.index.max())
    calendar = pd.date_range(fng.index.min(), last_day, freq="D")

    frame = pd.DataFrame(index=calendar)
    frame["fng"] = fng["value"].reindex(calendar).ffill()
    frame["cls"] = fng["classification"].reindex(calendar).ffill()
    frame["btc"] = btc.reindex(calendar).ffill()
    frame["mstr_trading"] = mstr.reindex(calendar).notna()
    frame["mstr"] = mstr.reindex(calendar).ffill()
    frame = frame.dropna(subset=["fng", "btc", "mstr"])
    return frame


# ---------------------------------------------------------------------------
# シグナル生成
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    """切替ルール。label モードは API の分類ラベル、numeric モードは数値しきい値を使う。"""

    name: str
    mode: str = "label"  # "label" | "numeric"
    fear_labels: tuple[str, ...] = (EXTREME_FEAR,)
    greed_labels: tuple[str, ...] = (EXTREME_GREED,)
    lo: int = 25
    hi: int = 75
    confirm: int = 1  # ゾーンに連続何日いたら確定するか


def build_target(frame: pd.DataFrame, rule: Rule) -> pd.Series:
    """各日の「望ましいポジション」（MSTR / BTC / NaN=変更なし）を返す。"""
    if rule.mode == "label":
        in_fear = frame["cls"].isin(rule.fear_labels)
        in_greed = frame["cls"].isin(rule.greed_labels)
    elif rule.mode == "numeric":
        in_fear = frame["fng"] <= rule.lo
        in_greed = frame["fng"] >= rule.hi
    else:
        raise ValueError(f"unknown mode: {rule.mode}")

    def confirmed(flag: pd.Series) -> pd.Series:
        return flag.astype(int).rolling(rule.confirm).sum().eq(rule.confirm)

    target = pd.Series(np.nan, index=frame.index, dtype=object)
    target[confirmed(in_fear)] = MSTR
    target[confirmed(in_greed)] = BTC
    return target


# ---------------------------------------------------------------------------
# シミュレーション
# ---------------------------------------------------------------------------
@dataclass
class Result:
    name: str
    equity: pd.Series
    daily_return: pd.Series
    position: pd.Series
    switches: list[tuple[pd.Timestamp, str, str]]
    initial_position: str


def simulate(
    frame: pd.DataFrame,
    target: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float = 0.0,
    inverse: bool = False,
    name: str = "strategy",
) -> Result:
    if inverse:  # 逆張り版: 恐怖で BTC、強欲で MSTR
        target = target.map({MSTR: BTC, BTC: MSTR})

    window = frame.loc[start:end].copy()
    if window.empty:
        raise ValueError("指定期間にデータがありません")

    prior = target.loc[: start - pd.Timedelta(days=1)].dropna()
    initial = str(prior.iloc[-1]) if len(prior) else BTC

    desired = target.loc[window.index].copy()
    if pd.isna(desired.iloc[0]):
        desired.iloc[0] = initial
    desired = desired.ffill()

    positions: list[str] = []
    switches: list[tuple[pd.Timestamp, str, str]] = []
    position = initial
    for day, wanted in desired.items():
        # 切替は MSTR の売買を伴うので、米国市場の営業日にしか実行できない
        if wanted != position and bool(window.at[day, "mstr_trading"]):
            switches.append((day, position, wanted))
            position = wanted
        positions.append(position)
    window["pos"] = positions  # その日の終値以降に保有する資産

    ret_btc = window["btc"].pct_change()
    ret_mstr = window["mstr"].pct_change()
    held_before = window["pos"].shift(1)
    daily = pd.Series(
        np.where(held_before.eq(MSTR), ret_mstr, ret_btc), index=window.index
    ).fillna(0.0)
    switched = window["pos"].ne(held_before) & held_before.notna()
    daily = daily - switched.astype(float) * (cost_bps / 1e4)  # 切替日にコストを差し引く
    equity = (1.0 + daily).cumprod()
    return Result(name, equity, daily, window["pos"], switches, initial)


def buy_and_hold(frame: pd.DataFrame, column: str, start, end, name: str) -> Result:
    window = frame.loc[start:end]
    daily = window[column].pct_change().fillna(0.0)
    equity = (1.0 + daily).cumprod()
    position = pd.Series(column.upper(), index=window.index)
    return Result(name, equity, daily, position, [], column.upper())


# ---------------------------------------------------------------------------
# 評価指標
# ---------------------------------------------------------------------------
def metrics(result: Result) -> dict:
    equity, daily = result.equity, result.daily_return
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = float(equity.iloc[-1] / equity.iloc[0])
    drawdown = equity / equity.cummax() - 1.0
    std = float(daily.std())
    return {
        "name": result.name,
        "multiple": total,
        "cagr": total ** (1 / years) - 1.0,
        "max_drawdown": float(drawdown.min()),
        "volatility": std * math.sqrt(365),
        "sharpe": (float(daily.mean()) / std * math.sqrt(365)) if std > 0 else float("nan"),
        "switches": len(result.switches),
        "mstr_share": float(result.position.eq(MSTR).mean()),
        "initial": result.initial_position,
    }


def legs(frame: pd.DataFrame, result: Result) -> pd.DataFrame:
    """保有区間ごとに、保有資産と「持たなかった方」のリターンを比べる。"""
    index = result.equity.index
    boundaries = [index[0]] + [day for day, _, _ in result.switches] + [index[-1]]
    rows = []
    for i in range(len(boundaries) - 1):
        t0, t1 = boundaries[i], boundaries[i + 1]
        if t0 == t1:
            continue
        held = result.position.loc[t0]
        other = BTC if held == MSTR else MSTR
        held_ret = frame.at[t1, held.lower()] / frame.at[t0, held.lower()] - 1.0
        other_ret = frame.at[t1, other.lower()] / frame.at[t0, other.lower()] - 1.0
        rows.append(
            {
                "from": t0.date(),
                "to": t1.date(),
                "days": (t1 - t0).days,
                "fng_at_entry": int(frame.at[t0, "fng"]),
                "held": held,
                "held_return": held_ret,
                "other_return": other_ret,
                "excess": held_ret - other_ret,
            }
        )
    return pd.DataFrame(rows)


def yearly_returns(results: list[Result]) -> pd.DataFrame:
    table = {}
    for result in results:
        equity = result.equity
        year_end = equity.groupby(equity.index.year).last()
        year_start = year_end.shift(1)
        year_start.iloc[0] = equity.iloc[0]
        table[result.name] = year_end / year_start - 1.0
    return pd.DataFrame(table)


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def fmt_pct(value: float) -> str:
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:+.1f}%"


def summary_table(rows: list[dict]) -> str:
    header = "| 戦略 | 最終倍率 | 年率 | 最大下落 | 年率ボラ | シャープ | 切替回数 | MSTR保有比率 | 初期 |\n|---|---|---|---|---|---|---|---|---|"
    lines = [header]
    for m in rows:
        lines.append(
            f"| {m['name']} | {m['multiple']:.2f}倍 | {fmt_pct(m['cagr'])} | {fmt_pct(m['max_drawdown'])} | "
            f"{m['volatility'] * 100:.0f}% | {m['sharpe']:.2f} | {m['switches']} | {m['mstr_share'] * 100:.0f}% | {m['initial']} |"
        )
    return "\n".join(lines)


def legs_table(table: pd.DataFrame) -> str:
    header = "| 期間 | 日数 | 入口の指数 | 保有 | 保有資産 | 持たなかった方 | 差 |\n|---|---|---|---|---|---|---|"
    lines = [header]
    for _, r in table.iterrows():
        lines.append(
            f"| {r['from']} 〜 {r['to']} | {r['days']} | {r['fng_at_entry']} | {r['held']} | "
            f"{fmt_pct(r['held_return'])} | {fmt_pct(r['other_return'])} | {fmt_pct(r['excess'])} |"
        )
    return "\n".join(lines)


def make_charts(frame: pd.DataFrame, results: dict[str, Result], out_dir: Path, title: str) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    matplotlib.rcParams["font.family"] = "IPAGothic"
    matplotlib.rcParams["axes.unicode_minus"] = False

    strategy = results["strategy"]
    index = strategy.equity.index
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.4]}
    )
    fig.suptitle(title, fontsize=14)

    colors = {"strategy": "#e4572e", "btc": "#f7931a", "mstr": "#4a7bd0", "inverse": "#888888"}
    labels = {"strategy": "恐怖→MSTR / 強欲→BTC（動画のルール）", "btc": "BTC 持ち切り", "mstr": "MSTR 持ち切り", "inverse": "逆ルール（恐怖→BTC / 強欲→MSTR）"}
    for key in ("btc", "mstr", "inverse", "strategy"):
        if key in results:
            ax1.plot(index, results[key].equity, color=colors[key], lw=2.2 if key == "strategy" else 1.3,
                     ls="--" if key == "inverse" else "-", label=labels[key])
    # MSTR 保有区間を塗る
    in_mstr = strategy.position.eq(MSTR)
    ax1.fill_between(index, 0, 1, where=in_mstr, transform=ax1.get_xaxis_transform(),
                     color="#4a7bd0", alpha=0.08, label="戦略が MSTR を保有している期間")
    ax1.set_yscale("log")
    ax1.set_ylabel("資産倍率（開始=1、対数目盛）")
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend(loc="upper left", fontsize=9)

    ax2.plot(index, frame.loc[index, "fng"], color="#333333", lw=1)
    ax2.axhspan(0, 25, color="#d7263d", alpha=0.12)
    ax2.axhspan(75, 100, color="#1b998b", alpha=0.12)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Fear & Greed")
    for day, before, after in strategy.switches:
        ax2.plot(day, frame.at[day, "fng"], marker="^" if after == MSTR else "v",
                 color="#4a7bd0" if after == MSTR else "#f7931a", ms=9, ls="none")
    ax2.plot([], [], marker="^", color="#4a7bd0", ls="none", label="MSTR へ切替")
    ax2.plot([], [], marker="v", color="#f7931a", ls="none", label="BTC へ切替")
    ax2.legend(loc="upper left", fontsize=9, ncol=2)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    path1 = out_dir / "backtest_equity.png"
    fig.savefig(path1, dpi=130)
    plt.close(fig)

    # 保有区間ごとの超過リターン
    table = legs(frame, strategy)
    fig, ax = plt.subplots(figsize=(12, 4.5))
    bars = ax.bar(range(len(table)), table["excess"] * 100,
                  color=["#1b998b" if v >= 0 else "#d7263d" for v in table["excess"]])
    ax.set_xticks(range(len(table)))
    ax.set_xticklabels([f"{r['from']:%y/%m}\n{r['held']}" if hasattr(r['from'], 'strftime') else f"{r['from']}\n{r['held']}" for _, r in table.iterrows()], fontsize=8)
    ax.axhline(0, color="#333", lw=0.8)
    ax.set_ylabel("保有資産 − 持たなかった方（%ポイント）")
    ax.set_title("保有区間ごとに「切替が正解だったか」（正なら正解）")
    for bar, value in zip(bars, table["excess"] * 100):
        ax.annotate(f"{value:+.0f}", (bar.get_x() + bar.get_width() / 2, value),
                    ha="center", va="bottom" if value >= 0 else "top", fontsize=8)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    path2 = out_dir / "backtest_legs.png"
    fig.savefig(path2, dpi=130)
    plt.close(fig)
    return [path1, path2]


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--start", default="2021-09-04")
    parser.add_argument("--end", default=None, help="省略時はデータの最終日")
    parser.add_argument("--cost-bps", type=float, default=30.0, help="切替1回あたりのコスト（bps）。既定 30 = 0.3%")
    parser.add_argument("--out", type=Path, default=Path("."))
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()

    frame = load_frame(args.data_dir)
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else frame.index[-1]
    end = min(end, frame.index[-1])
    if start < pd.Timestamp("2020-08-11"):
        print("注意: MSTR が BTC を買い始めたのは 2020-08-11 なので、それ以前は BTC 代替として意味を持ちません", file=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=True)

    base_rule = Rule("動画のルール（分類ラベル EF/EG）")
    target = build_target(frame, base_rule)
    results = {
        "strategy": simulate(frame, target, start, end, args.cost_bps, name="恐怖→MSTR / 強欲→BTC（動画）"),
        "strategy_nocost": simulate(frame, target, start, end, 0.0, name="同上（コスト0）"),
        "inverse": simulate(frame, target, start, end, args.cost_bps, inverse=True, name="逆ルール（恐怖→BTC / 強欲→MSTR）"),
        "btc": buy_and_hold(frame, "btc", start, end, "BTC 持ち切り"),
        "mstr": buy_and_hold(frame, "mstr", start, end, "MSTR 持ち切り"),
    }

    print(f"## 期間 {start.date()} 〜 {end.date()}  切替コスト {args.cost_bps:.0f}bps/回\n")
    print(summary_table([metrics(results[k]) for k in ("strategy", "strategy_nocost", "btc", "mstr", "inverse")]))
    print("\n### 年別リターン\n")
    yearly = yearly_returns([results["strategy"], results["btc"], results["mstr"]])
    print("| 年 | " + " | ".join(yearly.columns) + " |\n|---|" + "---|" * len(yearly.columns))
    for year, row in yearly.iterrows():
        print(f"| {year} | " + " | ".join(fmt_pct(v) for v in row) + " |")
    print("\n### 保有区間ごとの検証（動画のルール）\n")
    leg_table = legs(frame, results["strategy"])
    print(legs_table(leg_table))
    wins = int((leg_table["excess"] > 0).sum())
    print(f"\n正解だった区間: {wins} / {len(leg_table)}")

    # 感度分析
    print("\n### 感度分析（しきい値・確定日数・コスト）\n")
    grid_rules = [
        Rule("ラベル EF/EG", "label"),
        Rule("ラベル EF/EG 3日確定", "label", confirm=3),
        Rule("ラベル EF/EG 5日確定", "label", confirm=5),
        Rule("ラベル EF → Greed で戻す", "label", greed_labels=("Greed", EXTREME_GREED)),
        Rule("ラベル Fear → EG", "label", fear_labels=("Fear", EXTREME_FEAR)),
        Rule("数値 ≤20 / ≥80", "numeric", lo=20, hi=80),
        Rule("数値 ≤25 / ≥75", "numeric", lo=25, hi=75),
        Rule("数値 ≤30 / ≥70", "numeric", lo=30, hi=70),
        Rule("数値 ≤15 / ≥85", "numeric", lo=15, hi=85),
        Rule("数値 ≤10 / ≥90", "numeric", lo=10, hi=90),
    ]
    print("| ルール | コスト | 最終倍率 | 年率 | 最大下落 | 切替回数 | 対BTC倍率 |\n|---|---|---|---|---|---|---|")
    btc_multiple = metrics(results["btc"])["multiple"]
    grid_rows = []
    for rule in grid_rules:
        rule_target = build_target(frame, rule)
        for cost in (0.0, 30.0, 50.0):
            m = metrics(simulate(frame, rule_target, start, end, cost, name=rule.name))
            grid_rows.append({"rule": rule.name, "cost_bps": cost, **m})
            print(f"| {rule.name} | {cost:.0f} | {m['multiple']:.2f}倍 | {fmt_pct(m['cagr'])} | {fmt_pct(m['max_drawdown'])} | {m['switches']} | {m['multiple'] / btc_multiple:.2f}倍 |")

    summary = {
        "period": {"start": str(start.date()), "end": str(end.date())},
        "cost_bps": args.cost_bps,
        "metrics": [metrics(results[k]) for k in results],
        "legs": leg_table.assign(**{"from": leg_table["from"].astype(str), "to": leg_table["to"].astype(str)}).to_dict("records") if len(leg_table) else [],
        "grid": grid_rows,
        "yearly": {str(y): {k: (None if pd.isna(v) else float(v)) for k, v in row.items()} for y, row in yearly.iterrows()},
    }
    (args.out / "backtest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not args.no_charts:
        paths = make_charts(frame, results, args.out, f"Fear & Greed 切替戦略 バックテスト {start.date()} 〜 {end.date()}（コスト {args.cost_bps:.0f}bps/回）")
        print("\n図: " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
