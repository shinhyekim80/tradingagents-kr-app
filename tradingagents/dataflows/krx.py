"""Korean equity (KRX) data vendor for TradingAgents.

Serves KOSPI/KOSDAQ tickers from Korean sources that need no API key:

    prices / indicators   FinanceDataReader (Naver chart data)
    fundamentals          Naver Finance mobile API (PER/PBR/EPS/시총 등)
    news                  Naver Finance mobile API (한국어 기사)
    financial statements  delegated to yfinance with the .KS/.KQ suffix

Any symbol that is NOT Korean raises ``NoMarketDataError`` immediately
(no network call), so a vendor chain like ``"krx,yfinance"`` routes US
tickers straight through to yfinance.

Accepted Korean symbol forms:
    005930          bare 6-digit KRX code
    005930.KS       Yahoo-style KOSPI suffix
    247540.KQ       Yahoo-style KOSDAQ suffix
    삼성전자         Korean company name (resolved via cached KRX listing)

Place this file at: tradingagents/dataflows/krx.py
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from .config import get_config
from .errors import NoMarketDataError

logger = logging.getLogger(__name__)

_KR_CODE = re.compile(r"^(\d{6})(\.(KS|KQ))?$")
_HAS_HANGUL = re.compile(r"[가-힣]")

_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com",
}

_LISTING_MAX_AGE_DAYS = 7
_listing_cache: pd.DataFrame | None = None


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def _listing_path() -> str:
    cache_dir = get_config().get("data_cache_dir") or os.path.join(
        os.path.expanduser("~"), ".tradingagents", "cache"
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "krx_listing.csv")


def _load_listing() -> pd.DataFrame | None:
    """KRX listing (Code/Name/Market) with a weekly on-disk cache."""
    global _listing_cache
    if _listing_cache is not None:
        return _listing_cache

    path = _listing_path()
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < _LISTING_MAX_AGE_DAYS * 86400:
            try:
                _listing_cache = pd.read_csv(path, dtype={"Code": str})
                return _listing_cache
            except Exception:
                pass  # corrupt cache — refetch below

    try:
        import FinanceDataReader as fdr

        df = fdr.StockListing("KRX")
        # Column names vary across FDR versions.
        cols = {c.lower(): c for c in df.columns}
        code_col = cols.get("code") or cols.get("symbol")
        name_col = cols.get("name")
        market_col = cols.get("market")
        if not (code_col and name_col and market_col):
            raise ValueError(f"unexpected listing columns: {list(df.columns)}")
        df = df[[code_col, name_col, market_col]]
        df.columns = ["Code", "Name", "Market"]
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        df.to_csv(path, index=False)
        _listing_cache = df
        return df
    except Exception as e:
        logger.warning("KRX listing unavailable (%s); market suffix defaults to .KS", e)
        return None


def resolve_kr_symbol(symbol: str) -> tuple[str, str] | None:
    """Return ``(code, yahoo_symbol)`` for a Korean symbol, else ``None``.

    ``code`` is the bare 6-digit KRX code; ``yahoo_symbol`` carries the
    correct ``.KS``/``.KQ`` suffix for yfinance delegation.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    s = symbol.strip()

    m = _KR_CODE.match(s.upper())
    if m:
        code = m.group(1)
        suffix = m.group(3)
        if suffix:
            return code, f"{code}.{suffix}"
        return code, f"{code}.{_market_suffix(code)}"

    # Korean company name, e.g. 삼성전자
    if _HAS_HANGUL.search(s):
        listing = _load_listing()
        if listing is not None:
            hit = listing[listing["Name"] == s]
            if not hit.empty:
                code = hit.iloc[0]["Code"]
                return code, f"{code}.{_market_suffix(code)}"
        raise NoMarketDataError(symbol, detail="한국 종목명을 코드로 해석하지 못했습니다. 6자리 종목코드로 입력하세요 (예: 005930).")

    return None


def _market_suffix(code: str) -> str:
    listing = _load_listing()
    if listing is not None:
        hit = listing[listing["Code"] == code]
        if not hit.empty:
            market = str(hit.iloc[0]["Market"]).upper()
            return "KQ" if "KOSDAQ" in market else "KS"
    return "KS"


def _require_kr(symbol: str) -> tuple[str, str]:
    resolved = resolve_kr_symbol(symbol)
    if resolved is None:
        # Not Korean — signal the router to try the next vendor (yfinance).
        raise NoMarketDataError(symbol, detail="not a Korean (KRX) symbol")
    return resolved


# ---------------------------------------------------------------------------
# Prices (OHLCV)
# ---------------------------------------------------------------------------

def _fdr_ohlcv(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import FinanceDataReader as fdr

    df = fdr.DataReader(code, start_date, end_date)
    if df is None or df.empty:
        raise NoMarketDataError(code, detail=f"no rows between {start_date} and {end_date}")
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].copy()
    df.index.name = "Date"
    return df


def get_kr_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """OHLCV for a Korean ticker, formatted like the yfinance vendor output."""
    code, _ = _require_kr(symbol)
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = _fdr_ohlcv(code, start_date, end_date)

    csv_string = data.round(2).to_csv()
    header = (
        f"# Stock data for {symbol} (KRX code {code}) "
        f"from {start_date} to {end_date}\n"
        f"# Source: KRX via FinanceDataReader. Prices in KRW.\n"
        f"# Total records: {len(data)}\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    return header + csv_string


# ---------------------------------------------------------------------------
# Technical indicators (stockstats over FDR data)
# ---------------------------------------------------------------------------

_SUPPORTED_INDICATORS = {
    "close_50_sma": "50 SMA: medium-term trend / dynamic support-resistance.",
    "close_200_sma": "200 SMA: long-term trend benchmark (golden/death cross).",
    "close_10_ema": "10 EMA: responsive short-term momentum average.",
    "macd": "MACD: momentum via EMA differences; watch crossovers/divergence.",
    "macds": "MACD Signal: EMA smoothing of MACD; crossover triggers.",
    "macdh": "MACD Histogram: gap between MACD and signal; momentum strength.",
    "rsi": "RSI: overbought/oversold momentum (70/30 thresholds).",
    "boll": "Bollinger middle (20 SMA): dynamic benchmark of price movement.",
    "boll_ub": "Bollinger upper band: potential overbought / breakout zone.",
    "boll_lb": "Bollinger lower band: potential oversold zone.",
    "atr": "ATR: average true range for volatility-based stops/sizing.",
    "vwma": "VWMA: volume-weighted moving average; trend + volume confirmation.",
    "mfi": "MFI: money flow index; volume-weighted overbought/oversold.",
}


def get_kr_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str:
    from stockstats import wrap

    code, _ = _require_kr(symbol)

    if indicator not in _SUPPORTED_INDICATORS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(_SUPPORTED_INDICATORS.keys())}"
        )

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_dt - timedelta(days=look_back_days)
    # Fetch extra history so long-window indicators (200 SMA) are warm.
    fetch_start = (before - timedelta(days=400)).strftime("%Y-%m-%d")

    data = _fdr_ohlcv(code, fetch_start, curr_date).reset_index()
    df = wrap(data)
    df[indicator]  # trigger stockstats calculation
    df["Date"] = pd.to_datetime(df["date"] if "date" in df.columns else df["Date"])
    values = {
        row["Date"].strftime("%Y-%m-%d"): row[indicator] for _, row in df.iterrows()
    }

    lines = []
    d = curr_dt
    while d >= before:
        key = d.strftime("%Y-%m-%d")
        if key in values:
            v = values[key]
            lines.append(f"{key}: {'N/A' if pd.isna(v) else v}")
        else:
            lines.append(f"{key}: N/A: Not a trading day (weekend or holiday)")
        d -= timedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _SUPPORTED_INDICATORS[indicator]
    )


# ---------------------------------------------------------------------------
# Fundamentals (Naver Finance mobile API)
# ---------------------------------------------------------------------------

def _naver_json(url: str):
    resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_kr_fundamentals(ticker: str, curr_date: str = None) -> str:
    code, yahoo = _require_kr(ticker)

    fields: list[tuple[str, object]] = []
    name = None
    try:
        basic = _naver_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
        if isinstance(basic, dict):
            name = basic.get("stockName")
            for label, key in [
                ("Close Price (KRW)", "closePrice"),
                ("Market", "stockExchangeType"),
            ]:
                v = basic.get(key)
                if isinstance(v, dict):
                    v = v.get("name") or v.get("nameKor")
                if v is not None:
                    fields.append((label, v))
    except Exception as e:
        logger.info("Naver basic endpoint failed for %s: %s", code, e)

    try:
        integration = _naver_json(
            f"https://m.stock.naver.com/api/stock/{code}/integration"
        )
        infos = integration.get("totalInfos", []) if isinstance(integration, dict) else []
        for item in infos:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("code") or ""
            value = item.get("value")
            if value not in (None, "", "N/A"):
                fields.append((str(key), value))
    except Exception as e:
        logger.info("Naver integration endpoint failed for %s: %s", code, e)

    if not fields:
        # Naver unreachable — let yfinance (with proper suffix) try instead.
        try:
            from .y_finance import get_fundamentals as yf_fundamentals

            return yf_fundamentals(yahoo, curr_date)
        except Exception:
            raise NoMarketDataError(ticker, code, "no fundamentals from Naver or Yahoo")

    lines = [f"{k}: {v}" for k, v in fields]
    title = f"# Fundamentals for {name or ticker} (KRX {code}) as of {curr_date or datetime.now().strftime('%Y-%m-%d')}"
    note = "Source: Naver Finance. 값 단위는 네이버 표기 기준(원, 배, % 등)."
    return f"{title}\n{note}\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# News (Naver Finance mobile API, Korean-language)
# ---------------------------------------------------------------------------

def _parse_naver_dt(raw) -> datetime | None:
    if raw is None:
        return None
    s = str(raw)
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:8], "%Y%m%d")
    except ValueError:
        return None


def _iter_news_items(payload):
    """Yield item dicts from the (occasionally reshaped) Naver news payload."""
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("news") or []
    if not isinstance(payload, list):
        return
    for entry in payload:
        if not isinstance(entry, (dict, list)):
            continue
        if isinstance(entry, dict) and "items" in entry and isinstance(entry["items"], list):
            for item in entry["items"]:
                if isinstance(item, dict):
                    yield item
        elif isinstance(entry, dict):
            yield entry


def get_kr_news(ticker: str, start_date: str, end_date: str) -> str:
    code, _ = _require_kr(ticker)
    limit = get_config().get("news_article_limit", 20)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

    articles = []
    seen_titles = set()
    try:
        for page in (1, 2, 3):
            payload = _naver_json(
                f"https://m.stock.naver.com/api/news/stock/{code}"
                f"?pageSize=20&page={page}"
            )
            got_any = False
            for item in _iter_news_items(payload):
                got_any = True
                dt = _parse_naver_dt(
                    item.get("datetime") or item.get("dt") or item.get("createdAt")
                )
                title = html.unescape(str(item.get("title", ""))).strip()
                office = item.get("officeName") or item.get("press") or ""
                body = html.unescape(str(item.get("body", ""))).strip()
                if not title or title in seen_titles:
                    continue
                if dt is not None and not (start_dt <= dt < end_dt):
                    continue
                seen_titles.add(title)
                articles.append((dt, title, office, body))
            if not got_any or len(articles) >= limit:
                break
    except Exception as e:
        raise NoMarketDataError(ticker, code, f"Naver news fetch failed: {e}")

    if not articles:
        raise NoMarketDataError(
            ticker, code, f"no Korean news between {start_date} and {end_date}"
        )

    articles.sort(key=lambda a: a[0] or datetime.min, reverse=True)
    blocks = []
    for dt, title, office, body in articles[:limit]:
        when = dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown date"
        block = f"### {title}\n({when} | {office})"
        if body:
            block += f"\n{body[:500]}"
        blocks.append(block)

    return (
        f"## {ticker} (KRX {code}) 뉴스, {start_date} ~ {end_date} "
        f"(네이버 금융, {len(blocks)}건):\n\n" + "\n\n".join(blocks)
    )


# ---------------------------------------------------------------------------
# Financial statements — delegate to yfinance with the proper .KS/.KQ suffix
# ---------------------------------------------------------------------------

def get_kr_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    _, yahoo = _require_kr(ticker)
    from .y_finance import get_balance_sheet as impl

    return impl(yahoo, freq, curr_date)


def get_kr_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    _, yahoo = _require_kr(ticker)
    from .y_finance import get_cashflow as impl

    return impl(yahoo, freq, curr_date)


def get_kr_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    _, yahoo = _require_kr(ticker)
    from .y_finance import get_income_statement as impl

    return impl(yahoo, freq, curr_date)
