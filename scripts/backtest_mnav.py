#!/usr/bin/env python3
"""mNAV 条件付き Fear & Greed 切替戦略のバックテスト（fear-greed-switcher）

backtest.py の戦略（極端な恐怖で MSTR、極端な強欲で BTC）に、MSTR の mNAV
（企業価値 ÷ 保有 BTC 価値。Strategy 社の公式定義）を条件として加えて検証する。

ルール:
  A  F&G のみ（元の方式）
  B  F&G + 上限:      極端な恐怖でも mNAV が「上限」を超えていれば MSTR に切り替えない（BTC のまま）
  C  B + 過熱で退出:  mNAV が「退出水準」以上になったら F&G に関係なく BTC に戻す
  D  mNAV のみ:       F&G を使わず、mNAV が下限以下で MSTR、退出水準以上で BTC
  E  相対 mNAV:       極端な恐怖のとき、mNAV が過去 N 日の中央値より低ければ MSTR、高ければ BTC
  F  出口に条件:      極端な恐怖で MSTR（元の方式どおり）。極端な強欲でも mNAV が「出口水準」未満なら
                      売らずに MSTR を持ち続ける。mNAV が「強制退出水準」以上なら F&G に関係なく BTC へ

評価期間:
  過去 5 年、全期間、サイクル区切り（後知恵）の上げ相場・下げ相場、
  BTC が 200 日移動平均より上にある期間（実運用で判定できる区切り）

使い方:
  python3 backtest_mnav.py [--cost-bps 30] [--mnav-kind mnav|mnav_mcap] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt  # noqa: E402

DATA_DIR = bt.DATA_DIR
MSTR_BTC_START = pd.Timestamp("2020-08-11")


# ---------------------------------------------------------------------------
# データ
# ---------------------------------------------------------------------------
def load_mnav(data_dir: Path, kind: str) -> pd.Series:
    raw = json.loads((data_dir / "mstr_mnav.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(raw["data"])
    frame["date"] = pd.to_datetime(frame["date"])
    series = frame.set_index("date")[kind].astype(float).sort_index()
    return series


def attach_mnav(frame: pd.DataFrame, mnav: pd.Series) -> pd.DataFrame:
    frame = frame.copy()
    frame["mnav"] = mnav.reindex(frame.index).ffill()
    frame["mnav_median_1y"] = frame["mnav"].rolling(365, min_periods=120).median()
    return frame


# ---------------------------------------------------------------------------
# ルール
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MnavRule:
    name: str
    kind: str  # "A" | "B" | "C" | "D" | "E"
    entry_max: float = math.inf  # B/C: この mNAV 以下のときだけ MSTR へ
    exit_min: float = math.inf  # C/D: この mNAV 以上なら BTC へ
    entry_low: float = 1.0  # D: この mNAV 以下で MSTR へ
    exit_min_on_greed: float = 0.0  # F: 極端な強欲のとき、この mNAV 以上なら BTC へ
    lookback: int = 365  # E: 中央値を取る日数
    factor: float = 1.0  # E: 中央値に掛ける係数


def build_target(frame: pd.DataFrame, rule: MnavRule) -> pd.Series:
    fear = frame["cls"].eq(bt.EXTREME_FEAR)
    greed = frame["cls"].eq(bt.EXTREME_GREED)
    mnav = frame["mnav"]
    if rule.kind == "A":
        to_mstr, to_btc = fear, greed
    elif rule.kind == "B":
        to_mstr, to_btc = fear & (mnav <= rule.entry_max), greed
    elif rule.kind == "C":
        to_mstr, to_btc = fear & (mnav <= rule.entry_max), greed | (mnav >= rule.exit_min)
    elif rule.kind == "D":
        to_mstr, to_btc = mnav <= rule.entry_low, mnav >= rule.exit_min
    elif rule.kind == "E":
        median = mnav.rolling(rule.lookback, min_periods=max(60, rule.lookback // 3)).median() * rule.factor
        cheap = mnav < median
        to_mstr, to_btc = fear & cheap, greed | (fear & ~cheap & median.notna())
    elif rule.kind == "F":
        to_mstr, to_btc = fear, (greed & (mnav >= rule.exit_min_on_greed)) | (mnav >= rule.exit_min)
    else:
        raise ValueError(rule.kind)
    target = pd.Series(np.nan, index=frame.index, dtype=object)
    target[to_mstr.fillna(False)] = bt.MSTR
    target[to_btc.fillna(False)] = bt.BTC  # 同日に両方成立した場合は BTC（安全側）を優先
    return target


# ---------------------------------------------------------------------------
# 評価期間
# ---------------------------------------------------------------------------
def cycle_segments(frame: pd.DataFrame) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    btc = frame["btc"]
    top1 = btc.loc["2021"].idxmax()
    bottom1 = btc.loc[top1:"2023-06"].idxmin()
    top2 = btc.loc[bottom1:].idxmax()
    bottom2 = btc.loc[top2:].idxmin()
    end = frame.index[-1]
    return [
        ("上げ相場A", MSTR_BTC_START, top1),
        ("下げ相場A", top1, bottom1),
        ("上げ相場B", bottom1, top2),
        ("下げ相場B", top2, bottom2),
        ("反発（底〜現在）", bottom2, end),
    ]


def sma_bull_windows(frame: pd.DataFrame, window: int = 200, min_days: int = 30) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    btc = frame["btc"]
    bull = btc > btc.rolling(window).mean()
    runs: list[dict] = []
    current: dict | None = None
    for day, flag in bull.items():
        if day < MSTR_BTC_START or pd.isna(btc.rolling(window).mean().get(day, np.nan)):
            continue
        if current is None or current["bull"] != bool(flag):
            current = {"bull": bool(flag), "start": day, "end": day}
            runs.append(current)
        else:
            current["end"] = day
    return [(r["start"], r["end"]) for r in runs if r["bull"] and (r["end"] - r["start"]).days >= min_days]


# ---------------------------------------------------------------------------
# 評価
# ---------------------------------------------------------------------------
def evaluate(frame: pd.DataFrame, target: pd.Series, start, end, cost_bps: float) -> dict:
    strategy = bt.simulate(frame, target, start, end, cost_bps)
    btc = bt.buy_and_hold(frame, "btc", start, end, "btc")
    mstr = bt.buy_and_hold(frame, "mstr", start, end, "mstr")
    ms, mb, mm = bt.metrics(strategy), bt.metrics(btc), bt.metrics(mstr)
    return {
        "multiple": ms["multiple"],
        "btc_multiple": mb["multiple"],
        "mstr_multiple": mm["multiple"],
        "vs_btc": ms["multiple"] / mb["multiple"],
        "vs_mstr": ms["multiple"] / mm["multiple"],
        "max_drawdown": ms["max_drawdown"],
        "switches": ms["switches"],
        "mstr_share": ms["mstr_share"],
        "result": strategy,
    }


def bull_concat(frame: pd.DataFrame, target: pd.Series, windows, cost_bps: float) -> dict:
    """200日移動平均の上にいる期間だけ運用した場合の連結倍率。"""
    total = {"multiple": 1.0, "btc_multiple": 1.0, "mstr_multiple": 1.0, "switches": 0, "wins": 0, "n": 0}
    for start, end in windows:
        e = evaluate(frame, target, start, end, cost_bps)
        total["multiple"] *= e["multiple"]
        total["btc_multiple"] *= e["btc_multiple"]
        total["mstr_multiple"] *= e["mstr_multiple"]
        total["switches"] += e["switches"]
        total["wins"] += int(e["vs_btc"] > 1)
        total["n"] += 1
    total["vs_btc"] = total["multiple"] / total["btc_multiple"]
    total["vs_mstr"] = total["multiple"] / total["mstr_multiple"]
    return total


def fmt_x(value: float) -> str:
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# 図
# ---------------------------------------------------------------------------
def make_charts(frame: pd.DataFrame, results: dict[str, bt.Result], labels: dict[str, str], mnav_switches, out_dir: Path, title: str, mnav_label: str) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    matplotlib.rcParams["font.family"] = "IPAGothic"
    matplotlib.rcParams["axes.unicode_minus"] = False

    index = results["mnav"].equity.index
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 11), sharex=True, gridspec_kw={"height_ratios": [3, 1.3, 1.3]})
    fig.suptitle(title, fontsize=14)
    colors = {"mnav": "#1b998b", "d": "#8e44ad", "fg": "#e4572e", "btc": "#f7931a", "mstr": "#4a7bd0"}
    for key in ("btc", "mstr", "fg", "d", "mnav"):
        if key in results:
            ax1.plot(index, results[key].equity, color=colors[key], lw=2.2 if key == "mnav" else 1.3,
                     ls="--" if key == "d" else "-", label=labels[key])
    ax1.fill_between(index, 0, 1, where=results["mnav"].position.eq(bt.MSTR), transform=ax1.get_xaxis_transform(),
                     color="#1b998b", alpha=0.08, label="mNAV条件付きルールが MSTR を保有している期間")
    ax1.set_yscale("log")
    ax1.set_ylabel("資産倍率（開始=1、対数目盛）")
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend(loc="upper left", fontsize=9)

    ax2.plot(index, frame.loc[index, "mnav"], color="#333333", lw=1)
    ax2.plot(index, frame.loc[index, "mnav_median_1y"], color="#999999", lw=1, ls="--", label="過去1年の中央値")
    ax2.axhline(1.0, color="#d7263d", lw=0.8, ls=":")
    for day, before, after in mnav_switches:
        ax2.plot(day, frame.at[day, "mnav"], marker="^" if after == bt.MSTR else "v",
                 color="#4a7bd0" if after == bt.MSTR else "#f7931a", ms=9, ls="none")
    ax2.plot([], [], marker="^", color="#4a7bd0", ls="none", label="MSTR へ切替")
    ax2.plot([], [], marker="v", color="#f7931a", ls="none", label="BTC へ切替")
    ax2.set_ylabel(mnav_label)
    ax2.set_yscale("log")
    ax2.grid(True, which="both", alpha=0.3)
    ax2.legend(loc="upper right", fontsize=9, ncol=3)

    ax3.plot(index, frame.loc[index, "fng"], color="#333333", lw=1)
    ax3.axhspan(0, 25, color="#d7263d", alpha=0.12)
    ax3.axhspan(75, 100, color="#1b998b", alpha=0.12)
    ax3.set_ylim(0, 100)
    ax3.set_ylabel("Fear & Greed")
    ax3.grid(True, alpha=0.3)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    path = out_dir / "backtest_mnav_equity.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return [path]


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--mnav-kind", choices=["mnav", "mnav_mcap"], default="mnav",
                        help="mnav=企業価値ベース（公式定義）, mnav_mcap=時価総額ベース")
    parser.add_argument("--out", type=Path, default=Path("."))
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    frame = attach_mnav(bt.load_frame(args.data_dir), load_mnav(args.data_dir, args.mnav_kind))
    end = frame.index[-1]
    five_years = pd.Timestamp("2021-09-04")
    segments = cycle_segments(frame)
    bull_windows = sma_bull_windows(frame)
    cost = args.cost_bps

    print(f"## mNAV 条件付き検証  mNAV 種別: {args.mnav_kind}  コスト {cost:.0f}bps/回  データ最終日 {end.date()}")
    print(f"現在の mNAV: {frame['mnav'].iloc[-1]:.2f}（過去1年中央値 {frame['mnav_median_1y'].iloc[-1]:.2f}）、指数 {int(frame['fng'].iloc[-1])} {frame['cls'].iloc[-1]}\n")

    # --- ルール一覧（グリッド） ---
    rules: list[MnavRule] = [MnavRule("A: F&G のみ（元の方式）", "A")]
    for cap in (1.0, 1.25, 1.5, 2.0, 2.5):
        rules.append(MnavRule(f"B: F&G + 入口上限 {cap}", "B", entry_max=cap))
    for cap, exit_min in ((1.5, 2.0), (1.5, 2.5), (2.0, 2.5), (2.0, 3.0)):
        rules.append(MnavRule(f"C: F&G + 入口上限 {cap} / 強制退出 {exit_min}", "C", entry_max=cap, exit_min=exit_min))
    for low in (0.6, 0.8, 1.0, 1.25, 1.5):
        for exit_min in (2.0, 2.5, 3.0, 3.5):
            rules.append(MnavRule(f"D: mNAV のみ 下限 {low} / 退出 {exit_min}", "D", entry_low=low, exit_min=exit_min))
    for lookback in (180, 365, 730):
        for factor in (0.8, 1.0, 1.2):
            rules.append(MnavRule(f"E: 相対 mNAV N={lookback} 係数{factor}", "E", lookback=lookback, factor=factor))
    for threshold in (1.0, 1.25, 1.5, 2.0, 2.5):
        for exit_min in (3.0, math.inf):
            label = f"F: 強欲時の出口 mNAV {threshold} 以上 / 強制退出 {'なし' if exit_min == math.inf else exit_min}"
            rules.append(MnavRule(label, "F", exit_min_on_greed=threshold, exit_min=exit_min))

    bull_names = [s for s in segments if s[0].startswith("上げ")]
    print("### ルール別の成績（倍率。「対BTC」は BTC 持ち切りを 1 としたときの比）\n")
    header = "| ルール | 5年 | 5年 対BTC | 全期間 | 全期間 対BTC | 全期間 対MSTR | 最大下落 | 切替 | " + " | ".join(f"{n} 対BTC" for n, _, _ in bull_names) + " | 200日MA上のみ 対BTC | 同 対MSTR |"
    print(header)
    print("|" + "---|" * (header.count("|") - 1))
    grid_rows = []
    targets: dict[str, pd.Series] = {}
    for rule in rules:
        target = build_target(frame, rule)
        targets[rule.name] = target
        five = evaluate(frame, target, five_years, end, cost)
        full = evaluate(frame, target, MSTR_BTC_START, end, cost)
        bulls = [evaluate(frame, target, s0, s1, cost) for _, s0, s1 in bull_names]
        concat = bull_concat(frame, target, bull_windows, cost)
        row = {
            "rule": rule.name, "five": five["multiple"], "five_vs_btc": five["vs_btc"],
            "full": full["multiple"], "full_vs_btc": full["vs_btc"], "full_vs_mstr": full["vs_mstr"],
            "full_mdd": full["max_drawdown"], "switches": full["switches"],
            "bull_vs_btc": [b["vs_btc"] for b in bulls], "sma_vs_btc": concat["vs_btc"], "sma_vs_mstr": concat["vs_mstr"],
            "sma_multiple": concat["multiple"], "sma_switches": concat["switches"],
        }
        grid_rows.append(row)
        print(f"| {rule.name} | {fmt_x(five['multiple'])} | {fmt_x(five['vs_btc'])} | {fmt_x(full['multiple'])} | {fmt_x(full['vs_btc'])} | {fmt_x(full['vs_mstr'])} | {bt.fmt_pct(full['max_drawdown'])} | {full['switches']} | "
              + " | ".join(fmt_x(b["vs_btc"]) for b in bulls) + f" | {fmt_x(concat['vs_btc'])} | {fmt_x(concat['vs_mstr'])} |")

    print("\n参考: BTC 持ち切り 5年 {:.2f}倍 / 全期間 {:.2f}倍、MSTR 持ち切り 5年 {:.2f}倍 / 全期間 {:.2f}倍".format(
        evaluate(frame, targets[rules[0].name], five_years, end, cost)["btc_multiple"],
        evaluate(frame, targets[rules[0].name], MSTR_BTC_START, end, cost)["btc_multiple"],
        evaluate(frame, targets[rules[0].name], five_years, end, cost)["mstr_multiple"],
        evaluate(frame, targets[rules[0].name], MSTR_BTC_START, end, cost)["mstr_multiple"]))

    # --- 200日MA 上の各期間（ルール A と選抜ルール） ---
    print("\n### BTC が 200 日移動平均より上の期間（30 日以上）ごとの成績\n")
    picked_names = [
        "A: F&G のみ（元の方式）",
        "F: 強欲時の出口 mNAV 1.5 以上 / 強制退出 なし",
        "F: 強欲時の出口 mNAV 2.0 以上 / 強制退出 なし",
        "D: mNAV のみ 下限 1.0 / 退出 3.0",
        "E: 相対 mNAV N=365 係数1.0",
    ]
    picked = [r for r in rules if r.name in picked_names]
    print("| 期間 | 日数 | BTC | MSTR | " + " | ".join(r.name for r in picked) + " |")
    print("|" + "---|" * (4 + len(picked)))
    for start, stop in bull_windows:
        cells = []
        base = None
        for r in picked:
            e = evaluate(frame, targets[r.name], start, stop, cost)
            base = e
            cells.append(bt.fmt_pct(e["multiple"] - 1))
        print(f"| {start.date()}〜{stop.date()} | {(stop - start).days} | {bt.fmt_pct(base['btc_multiple'] - 1)} | {bt.fmt_pct(base['mstr_multiple'] - 1)} | " + " | ".join(cells) + " |")

    # --- サイクル区切り（全区間） ---
    print("\n### サイクル区切り（後知恵）ごとの成績\n")
    print("| 区間 | 期間 | BTC | MSTR | " + " | ".join(r.name for r in picked) + " |")
    print("|" + "---|" * (4 + len(picked)))
    for name, s0, s1 in segments:
        cells = []
        base = None
        for r in picked:
            e = evaluate(frame, targets[r.name], s0, s1, cost)
            base = e
            cells.append(bt.fmt_pct(e["multiple"] - 1))
        print(f"| {name} | {s0.date()}〜{s1.date()} | {bt.fmt_pct(base['btc_multiple'] - 1)} | {bt.fmt_pct(base['mstr_multiple'] - 1)} | " + " | ".join(cells) + " |")

    # --- 選抜ルールの詳細（区間別、開始日感度、現在の判定） ---
    chosen = next(r for r in rules if r.name == "F: 強欲時の出口 mNAV 1.5 以上 / 強制退出 なし")
    chosen_target = targets[chosen.name]
    full = evaluate(frame, chosen_target, MSTR_BTC_START, end, cost)
    print(f"\n### 選抜ルール『{chosen.name}』の保有区間（全期間）\n")
    legs = bt.legs(frame, full["result"])
    legs["mnav_at_entry"] = [round(float(frame.at[pd.Timestamp(d), "mnav"]), 2) for d in legs["from"]]
    print("| 期間 | 日数 | 実行日の指数 | 実行日の mNAV | 保有 | 保有資産 | 持たなかった方 | 差 |\n|---|---|---|---|---|---|---|---|")
    for _, r in legs.iterrows():
        print(f"| {r['from']} 〜 {r['to']} | {r['days']} | {r['fng_at_entry']} | {r['mnav_at_entry']} | {r['held']} | {bt.fmt_pct(r['held_return'])} | {bt.fmt_pct(r['other_return'])} | {bt.fmt_pct(r['excess'])} |")
    print(f"\n正解だった区間: {int((legs['excess'] > 0).sum())} / {len(legs)}")

    print("\n### 選抜ルールの開始日感度（終了日固定、対 BTC 持ち切り）\n")
    d_target = targets["D: mNAV のみ 下限 1.0 / 退出 3.0"]
    print("| 開始日 | A: F&G のみ 対BTC | 選抜ルール 対BTC | 選抜ルール 対MSTR | D 1.0/3.0 対BTC |\n|---|---|---|---|---|")
    wins_a = wins_c = wins_d = n = 0
    for start in pd.date_range("2020-10-01", "2025-07-01", freq="QS"):
        a = evaluate(frame, targets[rules[0].name], start, end, cost)
        c = evaluate(frame, chosen_target, start, end, cost)
        d = evaluate(frame, d_target, start, end, cost)
        n += 1; wins_a += a["vs_btc"] > 1; wins_c += c["vs_btc"] > 1; wins_d += d["vs_btc"] > 1
        print(f"| {start.date()} | {fmt_x(a['vs_btc'])} | {fmt_x(c['vs_btc'])} | {fmt_x(c['vs_mstr'])} | {fmt_x(d['vs_btc'])} |")
    print(f"\nBTC 持ち切りに勝った開始日: A {wins_a}/{n}、選抜ルール {wins_c}/{n}、D 1.0/3.0 {wins_d}/{n}")

    today_pos = full["result"].position.iloc[-1]
    print(f"\n### 現在の判定（{end.date()}）\n")
    print(f"- 選抜ルールのポジション: {today_pos}（最後の切替 {full['result'].switches[-1][0].date() if full['result'].switches else 'なし'}）")
    print(f"- mNAV {frame['mnav'].iloc[-1]:.2f}: 極端な強欲が出ても mNAV が {chosen.exit_min_on_greed} 未満なら MSTR を持ち続ける。{chosen.exit_min_on_greed} 以上で極端な強欲なら BTC へ")
    d_full = evaluate(frame, d_target, MSTR_BTC_START, end, cost)
    print(f"- D 1.0/3.0 のポジション: {d_full['result'].position.iloc[-1]}（最後の切替 {d_full['result'].switches[-1][0].date() if d_full['result'].switches else 'なし'}）。mNAV 3.0 以上で BTC へ")

    summary = {
        "mnav_kind": args.mnav_kind, "cost_bps": cost, "end": str(end.date()),
        "grid": grid_rows, "chosen": chosen.name,
        "bull_windows": [(str(a.date()), str(b.date())) for a, b in bull_windows],
        "segments": [(n, str(a.date()), str(b.date())) for n, a, b in segments],
    }
    (args.out / "backtest_mnav_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not args.no_charts:
        results = {
            "mnav": full["result"],
            "d": evaluate(frame, d_target, MSTR_BTC_START, end, cost)["result"],
            "fg": evaluate(frame, targets[rules[0].name], MSTR_BTC_START, end, cost)["result"],
            "btc": bt.buy_and_hold(frame, "btc", MSTR_BTC_START, end, "btc"),
            "mstr": bt.buy_and_hold(frame, "mstr", MSTR_BTC_START, end, "mstr"),
        }
        labels = {"mnav": f"{chosen.name}", "d": "D: mNAV のみ 下限 1.0 / 退出 3.0", "fg": "A: F&G のみ（動画のルール）", "btc": "BTC 持ち切り", "mstr": "MSTR 持ち切り"}
        mnav_label = "mNAV（時価総額÷保有BTC価値）" if args.mnav_kind == "mnav_mcap" else "mNAV（企業価値÷保有BTC価値）"
        paths = make_charts(frame, results, labels, full["result"].switches, args.out,
                            f"mNAV 条件付き切替戦略 {MSTR_BTC_START.date()} 〜 {end.date()}（コスト {cost:.0f}bps/回、{mnav_label}）", mnav_label)
        print("\n図: " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
