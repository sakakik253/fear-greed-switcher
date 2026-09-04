#!/usr/bin/env python3
"""研究用の追加銘柄取得（fear-greed-switcher）

引数で渡したティッカーの日足終値（分割調整済み）を Yahoo Finance から取得し、
data/extra/<ティッカー>.json に保存する。fetch_data.py の取得関数を再利用する。

使い方: python3 scripts/fetch_extra.py MSTU MSTX
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_data import fetch_yahoo, log  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "extra"


def main(symbols: list[str]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for symbol in symbols:
        try:
            rows = fetch_yahoo(symbol)
            payload = {"symbol": symbol, "source": "yahoo", "fetched_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"), "data": rows}
            (OUT_DIR / f"{symbol}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            log(f"{symbol}: {len(rows)} 行 ({rows[0]['date']} 〜 {rows[-1]['date']})")
        except Exception as error:  # noqa: BLE001
            failures += 1
            log(f"{symbol}: 取得失敗 {error}")
    return 1 if failures == len(symbols) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["MSTU"]))
