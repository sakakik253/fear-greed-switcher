#!/usr/bin/env python3
"""Strategy 社（MSTR）の mNAV 日次履歴取得スクリプト（fear-greed-switcher）

mnav.com の Strategy ページ（Next.js）に埋め込まれている日次データ（preparedData）を抽出し、
data/mstr_mnav.json に保存する。含まれるもの:
  - 株価、BTC 価格、保有 BTC 枚数、発行株数（基本・完全希薄化）
  - 時価総額、保有 BTC 価値（Bitcoin NAV）、負債、優先株、現金
  - mNAV（企業価値 ÷ Bitcoin NAV。Strategy 社の公式定義と同じ）、希薄化後 mNAV
  - 時価総額ベースの mNAV（このスクリプトで計算: 時価総額 ÷ Bitcoin NAV）
  - BTC 購入台帳

使い方:
  python3 fetch_mstr_fundamentals.py                 # Web から取得
  python3 fetch_mstr_fundamentals.py --from-file X   # 保存済み HTML から抽出（デバッグ用）

標準ライブラリのみで動作する（GitHub Actions の ubuntu-latest でそのまま実行可能）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SOURCE_URL = "https://www.mnav.com/mnav/strategy"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "mstr_mnav.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# preparedData.values のキー → 出力キー
FIELD_MAP = {
    "sharePrice": "share_price",
    "btcPrice": "btc_price",
    "btcHeld": "btc_held",
    "issuedShares": "issued_shares",
    "fullyDilutedShares": "diluted_shares",
    "marketCap": "market_cap",
    "bitcoinNav": "btc_nav",
    "mnav": "mnav",
    "mnavDiluted": "mnav_diluted",
    "totalDebt": "debt",
    "totalPreferredStock": "preferred",
    "totalCash": "cash",
    "btcPerShare": "btc_per_share",
}


def log(message: str) -> None:
    print(f"[mstr_mnav] {message}", flush=True)


def http_get(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_prepared_data(html_text: str) -> dict:
    """Next.js のストリーミングペイロード（self.__next_f.push）から preparedData を取り出す。"""
    decoder = json.JSONDecoder()
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html_text, flags=re.S):
        try:
            text = json.loads('"' + chunk + '"')  # JS 文字列リテラルとして復号
        except json.JSONDecodeError:
            continue
        marker = '"preparedData":'
        position = text.find(marker)
        while position >= 0:
            try:
                obj, _ = decoder.raw_decode(text, position + len(marker))
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and "timeline" in obj and "values" in obj:
                return obj
            position = text.find(marker, position + 1)
    raise RuntimeError("preparedData が見つかりません（ページ構造が変わった可能性）")


def build_timeline(prepared: dict, length: int) -> list[str]:
    """values 配列に対応する日付列を作る。fairValueRanges に日付があればそれを使い、無ければ暦日で補う。"""
    ranges = prepared.get("fairValueRanges") or []
    if isinstance(ranges, list) and len(ranges) == length and all("date" in r for r in ranges):
        return [str(r["date"])[:10] for r in ranges]
    start = date.fromisoformat(prepared["timeline"]["startDate"])
    end = date.fromisoformat(prepared["timeline"]["endDate"])
    days = (end - start).days + 1
    if days != length:
        raise RuntimeError(f"タイムライン長が一致しません: 暦日 {days} 日 vs 値 {length} 個")
    return [(start + timedelta(days=i)).isoformat() for i in range(length)]


def to_rows(prepared: dict) -> list[dict]:
    values = prepared["values"]
    length = len(values["sharePrice"])
    dates = build_timeline(prepared, length)
    rows = []
    for i, day in enumerate(dates):
        row: dict = {"date": day}
        for src, dst in FIELD_MAP.items():
            series = values.get(src)
            value = series[i] if isinstance(series, list) and i < len(series) else None
            if isinstance(value, float):
                value = round(value, 6 if dst in ("mnav", "mnav_diluted", "btc_per_share") else 4)
            row[dst] = value
        market_cap, btc_nav = row.get("market_cap"), row.get("btc_nav")
        row["mnav_mcap"] = round(market_cap / btc_nav, 6) if market_cap and btc_nav else None
        rows.append(row)
    return rows


def to_transactions(prepared: dict) -> list[dict]:
    result = []
    for tx in prepared.get("btcTransactions") or []:
        result.append(
            {
                "date": str(tx.get("date"))[:10],
                "type": tx.get("transactionType"),
                "btc_amount": tx.get("btcAmount"),
                "btc_held_after": tx.get("btcHeld"),
                "cost_usd": tx.get("acquisitionCost"),
                "cost_per_btc": round(tx["costPerBtc"], 2) if isinstance(tx.get("costPerBtc"), (int, float)) else None,
            }
        )
    result.sort(key=lambda t: t["date"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-file", type=Path, help="保存済み HTML から抽出する（デバッグ用）")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    try:
        html_text = args.from_file.read_text(encoding="utf-8", errors="replace") if args.from_file else http_get(SOURCE_URL)
        prepared = extract_prepared_data(html_text)
        rows = to_rows(prepared)
        transactions = to_transactions(prepared)
    except Exception as error:  # noqa: BLE001
        log(f"取得に失敗しました: {error}")
        return 1

    payload = {
        "source": SOURCE_URL,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "mnav_method": (prepared.get("metadata") or {}).get("mnavCalculationMethod"),
        "latest": {FIELD_MAP[k]: v for k, v in (prepared.get("latest") or {}).items() if k in FIELD_MAP},
        "data": rows,
        "transactions": transactions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log(f"{args.output.name}: {len(rows)} 日分 ({rows[0]['date']} 〜 {rows[-1]['date']}), 取引 {len(transactions)} 件, "
        f"最新 mNAV {rows[-1]['mnav']} / 時価総額ベース {rows[-1]['mnav_mcap']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
