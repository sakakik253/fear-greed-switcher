#!/usr/bin/env python3
"""ダッシュボードのビルド（fear-greed-switcher）

data/fng.json, data/prices.json, data/mstr_mnav.json を読み込み、
  - 判定ルール F（極端な恐怖で MSTR、極端な強欲でも mNAV が出口水準未満なら売らない）の日次ポジション
  - 比較用のルール A（F&G のみ）、BTC 持ち切り、MSTR 持ち切りの資産曲線
  - 200 日移動平均によるレジーム、mNAV の 1 年中央値
  - 最新日の判定（今日のアクション）と切替履歴
を計算して dist/ に静的サイトを組み立てる。標準ライブラリのみで動作する。

使い方: python3 scripts/build_site.py [--out dist]
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BTC, MSTR = "BTC", "MSTR"
# ルールのしきい値（mNAV は時価総額 ÷ 保有 BTC 価値）
THRESHOLDS = {"exit_on_greed": 1.5, "forced_exit": 3.0, "cheap": 1.0, "cost_bps": 30,
              "mstu_share": 0.5, "trend_band": 0.03, "mstr_ma_days": 100}
MSTU_DAILY_COST = 0.00142  # 日次 2 倍 ETF の実測コスト（MSTU 実データで較正。2倍複利の減価を除く分、年率およそ 36%）
ERA_START = date(2020, 8, 10)  # mnav.com のデータ開始日（Strategy 社の最初の BTC 購入日）
EQUITY_START = date(2020, 8, 11)
CLS_CODE = {"Extreme Fear": "EF", "Fear": "F", "Neutral": "N", "Greed": "G", "Extreme Greed": "EG"}


def log(message: str) -> None:
    print(f"[build_site] {message}", flush=True)


def load_json(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def to_date(text: str) -> date:
    return date.fromisoformat(text[:10])


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def ffill_series(points: dict[date, float], days: list[date]) -> list[float | None]:
    """日付→値の辞書を暦日リストに沿って前方補完する。"""
    result: list[float | None] = []
    last: float | None = None
    for day in days:
        if day in points:
            last = points[day]
        result.append(last)
    return result


def rolling_median(values: list[float | None], window: int, min_periods: int) -> list[float | None]:
    result: list[float | None] = []
    buffer: list[float] = []
    for i, value in enumerate(values):
        buffer.append(value)
        if len(buffer) > window:
            buffer.pop(0)
        valid = [v for v in buffer if v is not None]
        result.append(statistics.median(valid) if len(valid) >= min_periods else None)
    return result


def rolling_mean(values: list[float | None], window: int) -> list[float | None]:
    result: list[float | None] = []
    total = 0.0
    queue: list[float] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        queue.append(value)
        total += value
        if len(queue) > window:
            total -= queue.pop(0)
        result.append(total / len(queue) if len(queue) >= window else None)
    return result


# ---------------------------------------------------------------------------
# ルール
# ---------------------------------------------------------------------------
def targets_rule_f(cls: list[str], mnav: list[float | None], thresholds: dict) -> list[str | None]:
    """ルール F の「望ましいポジション」。None は変更なし。"""
    result: list[str | None] = []
    for c, m in zip(cls, mnav):
        to_btc = (c == "EG" and m is not None and m >= thresholds["exit_on_greed"]) or (m is not None and m >= thresholds["forced_exit"])
        to_mstr = c == "EF"
        result.append(BTC if to_btc else (MSTR if to_mstr else None))  # 同日に両方成立なら BTC を優先
    return result


def targets_rule_a(cls: list[str]) -> list[str | None]:
    return [BTC if c == "EG" else (MSTR if c == "EF" else None) for c in cls]


def simulate(targets: list[str | None], mstr_trading: list[bool], btc: list[float], mstr: list[float],
             equity_from: int, cost_bps: float) -> dict:
    """バックテストと同じ実行規則で日次ポジションと資産曲線を計算する。

    - 切替は MSTR の営業日の終値でのみ実行
    - ポジションはその日の終値以降に保有する資産
    - 資産曲線は equity_from の日を 1 とし、前日のポジションでその日のリターンを得る。切替日にコストを引く
    """
    n = len(targets)
    desired: list[str] = []
    positions: list[str] = []
    switches: list[dict] = []
    current_desired = targets[0] or BTC
    position = current_desired
    for i in range(n):
        if targets[i] is not None:
            current_desired = targets[i]
        desired.append(current_desired)
        if current_desired != position and mstr_trading[i]:
            switches.append({"index": i, "from": position, "to": current_desired})
            position = current_desired
        positions.append(position)

    equity: list[float | None] = [None] * n
    value = 1.0
    equity[equity_from] = value
    for i in range(equity_from + 1, n):
        held_before = positions[i - 1]
        if held_before == MSTR:
            ret = mstr[i] / mstr[i - 1] - 1.0
        else:
            ret = btc[i] / btc[i - 1] - 1.0
        if positions[i] != positions[i - 1]:
            ret -= cost_bps / 1e4
        value *= 1.0 + ret
        equity[i] = value
    return {"desired": desired, "positions": positions, "switches": switches, "equity": equity}


def buy_and_hold(prices: list[float], equity_from: int) -> list[float | None]:
    return [None if i < equity_from else prices[i] / prices[equity_from] for i in range(len(prices))]


def hysteresis_trend(prices: list[float], ma: list[float | None], band: float) -> list[bool]:
    """移動平均の +band 上で強気、-band 下で弱気に切り替える（その間は直前の判定を維持）。"""
    state = True
    result = []
    for price, m in zip(prices, ma):
        if m is not None:
            if price > m * (1 + band):
                state = True
            elif price < m * (1 - band):
                state = False
        result.append(state)
    return result


def portfolio_equity(weights_by_day: list[dict], returns: dict[str, list[float]], mstr_trading: list[bool],
                     equity_from: int, cost_bps: float) -> list[float | None]:
    """配分が変わった日（米国営業日）に目標配分へ入れ替えるポートフォリオの資産曲線。"""
    n = len(weights_by_day)
    equity: list[float | None] = [None] * n
    values: dict[str, float] = dict(weights_by_day[equity_from])
    current = tuple(sorted(values.items()))
    equity[equity_from] = 1.0
    for i in range(equity_from + 1, n):
        for asset in values:
            values[asset] *= 1.0 + returns[asset][i]
        target = weights_by_day[i]
        key = tuple(sorted(target.items()))
        if key != current and mstr_trading[i]:
            total = sum(values.values())
            new_values = {a: total * w for a, w in target.items()}
            turnover = sum(abs(new_values.get(a, 0.0) - values.get(a, 0.0)) for a in set(new_values) | set(values)) / 2 / total
            values = {a: v * (1.0 - turnover * cost_bps / 1e4) for a, v in new_values.items()}
            current = key
        equity[i] = sum(values.values())
    return equity


def max_drawdown(equity: list[float | None]) -> float:
    peak = 0.0
    worst = 0.0
    for value in equity:
        if value is None:
            continue
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def build(out_dir: Path) -> dict:
    fng_raw = load_json("fng.json")
    prices_raw = load_json("prices.json")
    mnav_raw = load_json("mstr_mnav.json")

    fng_points = {to_date(r["date"]): (int(r["value"]), CLS_CODE[r["classification"]]) for r in fng_raw["data"]}
    btc_points = {to_date(r["date"]): float(r["close"]) for r in prices_raw["btc"]["data"]}
    mstr_points = {to_date(r["date"]): float(r["close"]) for r in prices_raw["mstr"]["data"]}
    usdjpy_points = {to_date(r["date"]): float(r["close"]) for r in prices_raw.get("usdjpy", {}).get("data", [])}
    mnav_rows = {to_date(r["date"]): r for r in mnav_raw["data"]}
    mnav_last_day = max(mnav_rows)
    mnav_latest = mnav_rows[mnav_last_day]

    end = max(fng_points)  # 指数の最新日を「シグナル日」とする
    days = list(daterange(ERA_START, end))
    calendar_all_btc = list(daterange(min(btc_points), end))
    btc_all = ffill_series(btc_points, calendar_all_btc)
    sma_all = rolling_mean(btc_all, 200)
    sma_map = dict(zip(calendar_all_btc, sma_all))

    fng_vals = ffill_series({d: v[0] for d, v in fng_points.items()}, days)
    cls = ffill_series({d: v[1] for d, v in fng_points.items()}, days)
    btc = ffill_series(btc_points, days)
    mstr = ffill_series(mstr_points, days)
    mstr_trading = [d in mstr_points for d in days]
    usdjpy = ffill_series(usdjpy_points, days) if usdjpy_points else [None] * len(days)

    # mNAV: mnav.com の値を使い、それより後の日は最新の保有 BTC と発行株数から概算する
    mnav: list[float | None] = []
    mnav_official: list[float | None] = []
    last_m = last_o = None
    for i, d in enumerate(days):
        row = mnav_rows.get(d)
        if row and row.get("mnav_mcap") is not None:
            last_m = float(row["mnav_mcap"])
            last_o = float(row["mnav"]) if row.get("mnav") is not None else last_o
        elif d > mnav_last_day and mstr[i] and btc[i] and mnav_latest.get("issued_shares") and mnav_latest.get("btc_held"):
            market_cap = mstr[i] * mnav_latest["issued_shares"]
            btc_nav = mnav_latest["btc_held"] * btc[i]
            last_m = market_cap / btc_nav
            extras = (mnav_latest.get("debt") or 0) + (mnav_latest.get("preferred") or 0) - (mnav_latest.get("cash") or 0)
            last_o = (market_cap + extras) / btc_nav
        mnav.append(last_m)
        mnav_official.append(last_o)
    mnav_median = rolling_median(mnav, 365, 120)
    sma200 = [sma_map.get(d) for d in days]

    if any(v is None for v in (fng_vals[0], cls[0], btc[0], mstr[0], mnav[0])):
        raise RuntimeError("開始日のデータが揃っていません")

    trend_btc = hysteresis_trend(btc, sma200, THRESHOLDS["trend_band"])
    mstr_ma = rolling_mean(mstr, THRESHOLDS["mstr_ma_days"])
    trend_mstr = [m is not None and p > m for p, m in zip(mstr, mstr_ma)]

    equity_from = days.index(EQUITY_START)
    rule_f = simulate(targets_rule_f(cls, mnav, THRESHOLDS), mstr_trading, btc, mstr, equity_from, THRESHOLDS["cost_bps"])
    rule_a = simulate(targets_rule_a(cls), mstr_trading, btc, mstr, equity_from, THRESHOLDS["cost_bps"])
    eq_btc = buy_and_hold(btc, equity_from)
    eq_mstr = buy_and_hold(mstr, equity_from)

    # MSTU 参考戦略: ルール F が MSTR 側かつ BTC トレンド強気のときだけ MSTU を目標比率で持ち、それ以外は MSTR 100%
    r_mstr = [0.0] + [mstr[i] / mstr[i - 1] - 1.0 for i in range(1, len(mstr))]
    returns = {
        "MSTR": r_mstr,
        "MSTU": [2.0 * r - MSTU_DAILY_COST if t else 0.0 for r, t in zip(r_mstr, mstr_trading)],
    }
    share = THRESHOLDS["mstu_share"]
    mstu_weights = [
        {"MSTU": share, "MSTR": round(1.0 - share, 4)} if (rule_f["desired"][i] == MSTR and trend_btc[i]) else {"MSTR": 1.0}
        for i in range(len(days))
    ]
    eq_mstu_mix = portfolio_equity(mstu_weights, returns, mstr_trading, equity_from, THRESHOLDS["cost_bps"])
    eq_mstu_hold = portfolio_equity([{"MSTU": 1.0}] * len(days), returns, mstr_trading, equity_from, 0.0)

    # --- 最新日の判定 ---
    last = len(days) - 1
    prev = last - 1
    held_before_today = rule_f["positions"][prev]
    desired_today = rule_f["desired"][last]
    signal_fired_today = rule_f["desired"][last] != rule_f["desired"][prev]
    executed_today = rule_f["positions"][last] != rule_f["positions"][prev]
    zone_days = 1
    for i in range(last - 1, -1, -1):
        if cls[i] == cls[last]:
            zone_days += 1
        else:
            break
    mstr_close_day = max(d for d in mstr_points if d <= end)
    mstr_prev_day = max(d for d in mstr_points if d < mstr_close_day)
    bull = sma200[last] is not None and btc[last] > sma200[last]
    last_switch = rule_f["switches"][-1] if rule_f["switches"] else None
    leg_start = last_switch["index"] if last_switch else equity_from
    held = rule_f["positions"][last]
    other = BTC if held == MSTR else MSTR
    leg = {
        "since": days[leg_start].isoformat(),
        "days": last - leg_start,
        "held": held,
        "held_return": (mstr[last] / mstr[leg_start] - 1) if held == MSTR else (btc[last] / btc[leg_start] - 1),
        "other_return": (btc[last] / btc[leg_start] - 1) if held == MSTR else (mstr[last] / mstr[leg_start] - 1),
    }

    latest = {
        "date": end.isoformat(),
        "fng": fng_vals[last],
        "cls": cls[last],
        "fng_prev": fng_vals[prev],
        "cls_prev": cls[prev],
        "zone_days": zone_days,
        "btc_usd": round(btc[last], 2),
        "btc_prev": round(btc[prev], 2),
        "usdjpy": round(usdjpy[last], 3) if usdjpy[last] else None,
        "btc_jpy": round(btc[last] * usdjpy[last]) if usdjpy[last] else None,
        "mstr": round(mstr_points[mstr_close_day], 3),
        "mstr_prev": round(mstr_points[mstr_prev_day], 3),
        "mstr_date": mstr_close_day.isoformat(),
        "mnav": round(mnav[last], 4),
        "mnav_prev": round(mnav[prev], 4),
        "mnav_official": round(mnav_official[last], 4) if mnav_official[last] else None,
        "mnav_median_1y": round(mnav_median[last], 4) if mnav_median[last] else None,
        "mnav_date": mnav_last_day.isoformat(),
        "mnav_estimated": end > mnav_last_day,
        "btc_held": mnav_latest.get("btc_held"),
        "issued_shares": mnav_latest.get("issued_shares"),
        "market_cap": mnav_latest.get("market_cap"),
        "sma200": round(sma200[last], 2) if sma200[last] else None,
        "regime_bull": bull,
        "regime_gap": (btc[last] / sma200[last] - 1) if sma200[last] else None,
        "trend_btc_bull": trend_btc[last],
        "trend_btc_prev": trend_btc[prev],
        "sma200_upper": round(sma200[last] * (1 + THRESHOLDS["trend_band"]), 2) if sma200[last] else None,
        "sma200_lower": round(sma200[last] * (1 - THRESHOLDS["trend_band"]), 2) if sma200[last] else None,
        "mstr_ma": round(mstr_ma[last], 2) if mstr_ma[last] else None,
        "trend_mstr_up": trend_mstr[last],
        "trend_mstr_prev": trend_mstr[prev],
        "mstu_guide_share": share if (desired_today == MSTR and trend_btc[last]) else 0.0,
        "rule_position_before": held_before_today,
        "rule_target": desired_today,
        "rule_position": rule_f["positions"][last],
        "signal_fired_today": signal_fired_today,
        "executed_today": executed_today,
        "mstr_trading_today": mstr_trading[last],
        "leg": leg,
    }
    summary = {
        "since": EQUITY_START.isoformat(),
        "multiple_f": rule_f["equity"][last],
        "multiple_a": rule_a["equity"][last],
        "multiple_btc": eq_btc[last],
        "multiple_mstr": eq_mstr[last],
        "max_drawdown_f": max_drawdown(rule_f["equity"]),
        "max_drawdown_btc": max_drawdown(eq_btc),
        "max_drawdown_mstr": max_drawdown(eq_mstr),
        "switches_f": len(rule_f["switches"]),
        "multiple_mstu_mix": eq_mstu_mix[last],
        "max_drawdown_mstu_mix": max_drawdown(eq_mstu_mix),
        "multiple_mstu_hold": eq_mstu_hold[last],
        "max_drawdown_mstu_hold": max_drawdown(eq_mstu_hold),
        "mstu_daily_cost": MSTU_DAILY_COST,
    }
    switches = [
        {
            "date": days[s["index"]].isoformat(),
            "from": s["from"],
            "to": s["to"],
            "fng": fng_vals[s["index"]],
            "cls": cls[s["index"]],
            "mnav": round(mnav[s["index"]], 3),
            "btc": round(btc[s["index"]], 2),
            "mstr": round(mstr[s["index"]], 3),
        }
        for s in rule_f["switches"]
    ]

    def r(values, digits):
        return [None if v is None else round(v, digits) for v in values]

    dashboard = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "data_date": end.isoformat(),
        "thresholds": THRESHOLDS,
        "sources": {
            "fng": fng_raw.get("source"),
            "prices": {k: v.get("source") for k, v in prices_raw.items() if isinstance(v, dict)},
            "mnav": mnav_raw.get("source"),
            "fng_fetched_at": fng_raw.get("fetched_at"),
            "mnav_fetched_at": mnav_raw.get("fetched_at"),
        },
        "latest": latest,
        "summary": summary,
        "switches": switches,
        "series": {
            "date": [d.isoformat() for d in days],
            "fng": fng_vals,
            "cls": cls,
            "btc": r(btc, 2),
            "mstr": r(mstr, 3),
            "mt": [1 if t else 0 for t in mstr_trading],
            "mnav": r(mnav, 4),
            "mnav_med": r(mnav_median, 4),
            "sma200": r(sma200, 2),
            "trend_btc": [1 if t else 0 for t in trend_btc],
            "mstr_ma": r(mstr_ma, 2),
            "trend_mstr": [1 if t else 0 for t in trend_mstr],
            "pos": rule_f["positions"],
            "tgt": rule_f["desired"],
            "eq_f": r(rule_f["equity"], 4),
            "eq_a": r(rule_a["equity"], 4),
            "eq_btc": r(eq_btc, 4),
            "eq_mstr": r(eq_mstr, 4),
            "eq_mstu_mix": r(eq_mstu_mix, 4),
        },
    }

    # --- dist/ を組み立てる ---
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "data").mkdir(parents=True)
    dashboard_json = json.dumps(dashboard, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "data" / "dashboard.json").write_text(dashboard_json, encoding="utf-8")
    for name in ("fng.json", "prices.json", "mstr_mnav.json"):
        shutil.copy(DATA_DIR / name, out_dir / "data" / name)
    template = (ROOT / "index.html").read_text(encoding="utf-8")
    if "__DASHBOARD_JSON__" not in template:
        raise RuntimeError("index.html に __DASHBOARD_JSON__ プレースホルダーがありません")
    embedded = dashboard_json.replace("</", "<\\/")  # script 要素内で </script> にならないようにする
    (out_dir / "index.html").write_text(template.replace("__DASHBOARD_JSON__", embedded), encoding="utf-8")
    shutil.copy(ROOT / "manifest.webmanifest", out_dir / "manifest.webmanifest")
    shutil.copytree(ROOT / "assets", out_dir / "assets")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    log(f"データ最終日 {end}、MSTR 終値 {mstr_close_day}、mNAV データ {mnav_last_day}{'（以降は概算）' if latest['mnav_estimated'] else ''}")
    log(f"判定: 指数 {latest['fng']} {latest['cls']}、mNAV {latest['mnav']}、ルールの保有 {latest['rule_position']}、"
        f"望ましい {latest['rule_target']}、本日シグナル {'あり' if signal_fired_today else 'なし'}")
    log(f"切替回数 {len(switches)}、{EQUITY_START} からの倍率: F {summary['multiple_f']:.2f} / A {summary['multiple_a']:.2f} / "
        f"BTC {summary['multiple_btc']:.2f} / MSTR {summary['multiple_mstr']:.2f} / MSTU参考戦略 {summary['multiple_mstu_mix']:.2f} / MSTU持ち切り {summary['multiple_mstu_hold']:.2f}")
    log(f"トレンド: BTC 200日MA±{THRESHOLDS['trend_band']*100:.0f}% {'強気' if latest['trend_btc_bull'] else '弱気'}、"
        f"MSTR {THRESHOLDS['mstr_ma_days']}日MA {'上' if latest['trend_mstr_up'] else '下'}、MSTU 比率の目安 {latest['mstu_guide_share']*100:.0f}%")
    log(f"出力: {out_dir}（index.html {len(embedded) // 1024} KB のデータを埋め込み）")
    return dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        build(args.out)
    except Exception as error:  # noqa: BLE001
        log(f"ビルドに失敗しました: {error}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
