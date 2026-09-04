#!/usr/bin/env python3
"""市場データ取得スクリプト（fear-greed-switcher）

取得対象:
  1. Crypto Fear & Greed Index の全履歴（alternative.me）
  2. BTC-USD の日足終値（Yahoo Finance → CryptoCompare → Stooq の順でフォールバック）
  3. MSTR の日足終値・株式分割調整済み（Yahoo Finance → Stooq の順でフォールバック）
  4. USDJPY の日足（Yahoo Finance。取得できなくても処理は続行する）

出力先（このスクリプトの親フォルダ直下の data/）:
  data/fng.json     … {"source", "fetched_at", "data": [{"date", "value", "classification"}, ...]}
  data/prices.json  … {"fetched_at", "btc": {"source", "data": [{"date", "close"}, ...]}, "mstr": {...}, "usdjpy": {...}}

標準ライブラリのみで動作する（GitHub Actions の ubuntu-latest でそのまま実行可能）。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 4年周期の比較に使うため BTC は 2014 年（Yahoo の最古）から保持する。F&G 指数は 2018-02-01 開始
START_DATE = "2014-01-01"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    print(f"[fetch_data] {message}", flush=True)


def http_get(url: str, retries: int = 3, timeout: int = 30) -> str:
    """指数バックオフ付きの GET。失敗時は RuntimeError を投げる。"""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
            last_error = error
            log(f"  取得失敗 ({attempt}/{retries}) {url}: {error}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"GET failed: {url}: {last_error}")


def unix_to_date(unix_seconds: int | float) -> str:
    return datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc).date().isoformat()


def normalize_rows(rows: list[dict], value_key: str) -> list[dict]:
    """日付で重複排除・昇順ソートし、START_DATE 以降だけ残す。"""
    by_date: dict[str, dict] = {}
    for row in rows:
        if row["date"] >= START_DATE:
            by_date[row["date"]] = row  # 同一日付は後勝ち（最新の値を採用）
    result = [by_date[d] for d in sorted(by_date)]
    if not result:
        raise RuntimeError(f"有効な行がありません ({value_key})")
    return result


def ensure_daily(rows: list[dict], name: str) -> list[dict]:
    """日足であることを確認する。月足などが返ってきた場合は例外にしてフォールバックさせる。"""
    if len(rows) < 2:
        raise RuntimeError(f"{name}: 行数が不足しています ({len(rows)} 行)")
    span_days = (date.fromisoformat(rows[-1]["date"]) - date.fromisoformat(rows[0]["date"])).days
    rows_per_year = len(rows) / max(span_days / 365.25, 1e-9)
    if rows_per_year < 200:
        raise RuntimeError(f"{name}: 日足ではないようです（年あたり {rows_per_year:.0f} 行）")
    return rows


def try_sources(name: str, sources: list[tuple[str, callable]]) -> tuple[str, list[dict]]:
    """複数ソースを順に試し、最初に成功したものを返す。"""
    errors: list[str] = []
    for source_name, fetcher in sources:
        try:
            log(f"{name}: {source_name} から取得中...")
            rows = fetcher()
            log(f"{name}: {source_name} 成功 ({len(rows)} 行, {rows[0]['date']} 〜 {rows[-1]['date']})")
            return source_name, rows
        except Exception as error:  # noqa: BLE001 - フォールバックのため全例外を捕捉
            log(f"{name}: {source_name} 失敗: {error}")
            errors.append(f"{source_name}: {error}")
    raise RuntimeError(f"{name}: すべてのソースで失敗\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# 1. Fear & Greed Index
# ---------------------------------------------------------------------------
def fetch_fng_alternative_me() -> list[dict]:
    raw = json.loads(http_get("https://api.alternative.me/fng/?limit=0&format=json"))
    if raw.get("metadata", {}).get("error"):
        raise RuntimeError(f"API error: {raw['metadata']['error']}")
    rows = [
        {
            "date": unix_to_date(item["timestamp"]),
            "value": int(item["value"]),
            "classification": item["value_classification"],
        }
        for item in raw["data"]
    ]
    return normalize_rows(rows, "fng")


# ---------------------------------------------------------------------------
# 2./3. 価格データ
# ---------------------------------------------------------------------------
def fetch_yahoo(symbol: str) -> list[dict]:
    """Yahoo Finance chart API。adjclose（分割調整済み）を優先して使う。

    range=max を指定すると日足ではなく月足が返ることがあるため、期間は period1/period2 で明示する。
    """
    period1 = int(datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.now(tz=timezone.utc).timestamp()) + 86400
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
        f"?period1={period1}&period2={period2}&interval=1d&events=div%2Csplit"
    )
    raw = json.loads(http_get(url))
    chart = raw.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error: {chart['error']}")
    result = chart["result"][0]
    timestamps = result["timestamp"]
    indicators = result["indicators"]
    adjclose = indicators.get("adjclose", [{}])[0].get("adjclose")
    closes = adjclose if adjclose else indicators["quote"][0]["close"]
    rows = [
        {"date": unix_to_date(ts), "close": round(float(close), 4)}
        for ts, close in zip(timestamps, closes)
        if close is not None
    ]
    return ensure_daily(normalize_rows(rows, symbol), symbol)


def fetch_stooq(symbol: str) -> list[dict]:
    """Stooq の日足 CSV（Date,Open,High,Low,Close,Volume）。"""
    text = http_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d")
    if not text.lower().startswith("date"):
        raise RuntimeError(f"CSV ではない応答: {text[:80]!r}")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for record in reader:
        close = record.get("Close")
        if close in (None, "", "-"):
            continue
        rows.append({"date": record["Date"], "close": round(float(close), 4)})
    return ensure_daily(normalize_rows(rows, symbol), symbol)


def fetch_cryptocompare_btc() -> list[dict]:
    """CryptoCompare histoday。2000 日ずつ遡って START_DATE まで取得する。"""
    rows: list[dict] = []
    to_ts = int(datetime.now(tz=timezone.utc).timestamp())
    start_ts = int(datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc).timestamp())
    for _ in range(5):  # 5 回 × 2000 日 ≒ 27 年分で十分
        url = (
            "https://min-api.cryptocompare.com/data/v2/histoday"
            f"?fsym=BTC&tsym=USD&limit=2000&toTs={to_ts}"
        )
        raw = json.loads(http_get(url))
        if raw.get("Response") != "Success":
            raise RuntimeError(f"API error: {raw.get('Message')}")
        batch = raw["Data"]["Data"]
        rows.extend(
            {"date": unix_to_date(item["time"]), "close": round(float(item["close"]), 4)}
            for item in batch
            if item.get("close")
        )
        oldest = min(item["time"] for item in batch)
        if oldest <= start_ts or len(batch) < 2000:
            break
        to_ts = oldest - 86400
    return ensure_daily(normalize_rows(rows, "BTC"), "BTC")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    failures: list[str] = []

    # Fear & Greed
    try:
        source, rows = try_sources("F&G", [("alternative.me", fetch_fng_alternative_me)])
        (DATA_DIR / "fng.json").write_text(
            json.dumps({"source": source, "fetched_at": fetched_at, "data": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as error:  # noqa: BLE001
        failures.append(f"fng: {error}")
        log(f"F&G の取得に失敗: {error}")

    # 価格
    prices: dict = {"fetched_at": fetched_at}
    optional_keys = {"usdjpy"}  # 取得失敗しても全体を失敗にしない
    for key, label, sources in (
        (
            "btc",
            "BTC-USD",
            [
                ("yahoo:BTC-USD", lambda: fetch_yahoo("BTC-USD")),
                ("cryptocompare", fetch_cryptocompare_btc),
                ("stooq:btcusd", lambda: fetch_stooq("btcusd")),
            ],
        ),
        (
            "mstr",
            "MSTR",
            [
                ("yahoo:MSTR", lambda: fetch_yahoo("MSTR")),
                ("stooq:mstr.us", lambda: fetch_stooq("mstr.us")),
            ],
        ),
        (
            "usdjpy",
            "USDJPY",
            [
                ("yahoo:JPY=X", lambda: fetch_yahoo("JPY=X")),
                ("stooq:usdjpy", lambda: fetch_stooq("usdjpy")),
            ],
        ),
    ):
        try:
            source, rows = try_sources(label, sources)
            prices[key] = {"source": source, "data": rows}
        except Exception as error:  # noqa: BLE001
            if key in optional_keys:
                log(f"{label} の取得に失敗（任意データなので続行）: {error}")
            else:
                failures.append(f"{key}: {error}")
                log(f"{label} の取得に失敗: {error}")

    if "btc" in prices or "mstr" in prices:
        (DATA_DIR / "prices.json").write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")

    log("---- 結果 ----")
    for path in sorted(DATA_DIR.glob("*.json")):
        log(f"{path.name}: {path.stat().st_size:,} bytes")
    if failures:
        log("失敗したデータ:\n  " + "\n  ".join(failures))
        return 1
    log("すべて取得できました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
