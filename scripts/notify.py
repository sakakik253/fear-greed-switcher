#!/usr/bin/env python3
"""ntfy 通知（fear-greed-switcher）

dist/data/dashboard.json を読み、前日と比べて次のいずれかが変わった日だけ通知する。
  1. ルール F の望ましいポジション（切替シグナル）… 優先度 高
  2. 指数の極端ゾーン（極端な恐怖・極端な強欲）への出入り
  3. mNAV のしきい値（1.0 / 1.5 / 3.0）の上抜け・下抜け
  4. MSTU 比率の目安（ルール F が MSTR 側かつ BTC トレンド強気なら目標比率、それ以外は 0%）の変化 … 優先度 高
  5. BTC トレンド判定（200 日移動平均±3%）の転換、MSTR の 100 日移動平均の上抜け・下抜け

環境変数:
  NTFY_TOPIC     通知先トピック（未設定なら何もしない）
  NTFY_SERVER    既定 https://ntfy.sh
  DASHBOARD_URL  通知から開くダッシュボードの URL

使い方: python3 scripts/notify.py [--test] [--dashboard dist/data/dashboard.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = {"EF": "極端な恐怖", "F": "恐怖", "N": "中立", "G": "強欲", "EG": "極端な強欲"}


def log(message: str) -> None:
    print(f"[notify] {message}", flush=True)


def publish(server: str, topic: str, title: str, message: str, priority: int, tags: list[str], click: str | None) -> None:
    payload = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags}
    if click:
        payload["click"] = click
    request = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "fear-greed-switcher notify/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        log(f"送信 {response.status}: {title}")


def detect_events(dashboard: dict) -> list[dict]:
    s = dashboard["series"]
    latest = dashboard["latest"]
    th = dashboard["thresholds"]
    i, j = len(s["date"]) - 1, len(s["date"]) - 2
    events: list[dict] = []

    if s["tgt"][i] != s["tgt"][j]:
        to = s["tgt"][i]
        when = "本日の終値" if latest["mstr_trading_today"] else "次の米国営業日の終値"
        events.append(
            {
                "title": f"切替シグナル: {s['tgt'][j]} から {to} へ",
                "message": (
                    f"指数 {latest['fng']}（{LABEL[latest['cls']]}）、mNAV {latest['mnav']:.2f}。"
                    f"{when}で {to} に切り替える判定です。"
                ),
                "priority": 4,
                "tags": ["rotating_light", "arrows_counterclockwise"],
            }
        )

    extreme_now = s["cls"][i] if s["cls"][i] in ("EF", "EG") else None
    extreme_prev = s["cls"][j] if s["cls"][j] in ("EF", "EG") else None
    if extreme_now != extreme_prev:
        if extreme_now:
            events.append(
                {
                    "title": f"指数が{LABEL[extreme_now]}に入りました（{latest['fng']}）",
                    "message": f"前日 {latest['fng_prev']}（{LABEL[latest['cls_prev']]}）。ルールの保有は {latest['rule_position']}。",
                    "priority": 3,
                    "tags": ["chart_with_downwards_trend" if extreme_now == "EF" else "chart_with_upwards_trend"],
                }
            )
        else:
            events.append(
                {
                    "title": f"指数が{LABEL[extreme_prev]}を抜けました（{latest['fng']} {LABEL[latest['cls']]}）",
                    "message": f"前日 {latest['fng_prev']}。ルールの保有は {latest['rule_position']}。",
                    "priority": 2,
                    "tags": ["information_source"],
                }
            )

    m_now, m_prev = s["mnav"][i], s["mnav"][j]
    if m_now is not None and m_prev is not None:
        for name, level in (("割安の目安", th["cheap"]), ("強欲時の出口水準", th["exit_on_greed"]), ("強制退出水準", th["forced_exit"])):
            if (m_prev < level) != (m_now < level):
                direction = "上抜け" if m_now >= level else "下抜け"
                events.append(
                    {
                        "title": f"mNAV が {name} {level} を{direction}（{m_now:.2f}）",
                        "message": f"前日 {m_prev:.2f}。指数 {latest['fng']}（{LABEL[latest['cls']]}）。",
                        "priority": 3,
                        "tags": ["scales"],
                    }
                )

    # MSTU 比率の目安（ルール F が MSTR 側かつ BTC トレンド強気なら目標比率、それ以外は 0%）
    share = th.get("mstu_share", 0.5)
    guide_now = share if (s["tgt"][i] == "MSTR" and s["trend_btc"][i]) else 0.0
    guide_prev = share if (s["tgt"][j] == "MSTR" and s["trend_btc"][j]) else 0.0
    if guide_now != guide_prev:
        events.append(
            {
                "title": f"MSTU 比率の目安が {guide_prev * 100:.0f}% から {guide_now * 100:.0f}% に変わりました",
                "message": (
                    ("MSTU を減らして MSTR に寄せる判定です。" if guide_now < guide_prev else "MSTU を目安の比率まで戻してよい判定です。")
                    + f" 指数 {latest['fng']}（{LABEL[latest['cls']]}）、mNAV {latest['mnav']:.2f}、"
                    f"BTC トレンド {'強気' if s['trend_btc'][i] else '弱気'}。"
                ),
                "priority": 4,
                "tags": ["rotating_light", "chart_with_upwards_trend" if guide_now > guide_prev else "chart_with_downwards_trend"],
            }
        )

    if s["trend_btc"][i] != s["trend_btc"][j]:
        bull_now = bool(s["trend_btc"][i])
        band = th.get("trend_band", 0.03) * 100
        events.append(
            {
                "title": "BTC のトレンド判定が" + ("弱気から強気に転換" if bull_now else "強気から弱気に転換") + f"（200 日移動平均±{band:.0f}%）",
                "message": f"BTC {s['btc'][i]:,.0f} USD、200 日移動平均 {s['sma200'][i]:,.0f} USD。",
                "priority": 3,
                "tags": ["compass"],
            }
        )

    if s["trend_mstr"][i] != s["trend_mstr"][j] and s["mstr_ma"][i]:
        up_now = bool(s["trend_mstr"][i])
        events.append(
            {
                "title": f"MSTR が {th.get('mstr_ma_days', 100)} 日移動平均を" + ("上抜け" if up_now else "下抜け"),
                "message": f"MSTR {s['mstr'][i]:.2f} USD、移動平均 {s['mstr_ma'][i]:.2f} USD。",
                "priority": 2,
                "tags": ["information_source"],
            }
        )
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="現在の判定をテスト通知として送る")
    parser.add_argument("--dashboard", type=Path, default=ROOT / "dist" / "data" / "dashboard.json")
    args = parser.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip()
    click = os.environ.get("DASHBOARD_URL", "").strip() or None
    if not topic:
        log("NTFY_TOPIC が未設定なので通知をスキップします")
        return 0

    dashboard = json.loads(args.dashboard.read_text(encoding="utf-8"))
    latest = dashboard["latest"]
    events = detect_events(dashboard)
    if args.test:
        events.insert(
            0,
            {
                "title": "テスト通知: ダッシュボードの通知設定は有効です",
                "message": (
                    f"{latest['date']} 指数 {latest['fng']}（{LABEL[latest['cls']]}）、mNAV {latest['mnav']:.2f}、"
                    f"ルールの保有 {latest['rule_position']}、望ましいポジション {latest['rule_target']}。"
                ),
                "priority": 3,
                "tags": ["white_check_mark"],
            },
        )
    if not events:
        log(f"{latest['date']}: 前日から判定の変化はありません（通知なし）")
        return 0
    for event in events:
        publish(server, topic, event["title"], event["message"], event["priority"], event["tags"], click)
    return 0


if __name__ == "__main__":
    sys.exit(main())
