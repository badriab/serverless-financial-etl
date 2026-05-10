"""
lambdas/ingestor/api_client.py

Thin HTTP client for the Alpha Vantage TIME_SERIES_DAILY endpoint.
Retries on transient failures with exponential backoff.
"""

import logging
import time
from typing import List

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


def _parse_daily_series(symbol: str, raw: dict) -> List[dict]:
    """
    Parses Alpha Vantage TIME_SERIES_DAILY response into a flat list of
    OHLCV dicts, one per trading day.

    Alpha Vantage response shape:
    {
      "Time Series (Daily)": {
        "2025-05-08": {"1. open": "182.50", "2. high": "185.20", ...},
        ...
      }
    }
    """
    series = raw.get("Time Series (Daily)")
    if not series:
        error_msg = raw.get("Note") or raw.get("Information") or raw.get("Error Message")
        raise ValueError(
            f"No time series data for {symbol}. "
            f"Alpha Vantage response: {error_msg or 'unknown error'}"
        )

    records = []
    for date_str, values in series.items():
        records.append({
            "symbol": symbol,
            "date": date_str,
            "open": float(values["1. open"]),
            "high": float(values["2. high"]),
            "low": float(values["3. low"]),
            "close": float(values["4. close"]),
            "volume": float(values["5. volume"]),
        })

    # Sort ascending by date
    records.sort(key=lambda r: r["date"])
    return records


def fetch_daily_ohlcv(symbol: str, api_key: str, outputsize: str = "compact") -> List[dict]:
    """
    Fetches daily OHLCV data for a single symbol from Alpha Vantage.

    Args:
        symbol:     Ticker symbol e.g. "AAPL"
        api_key:    Alpha Vantage API key
        outputsize: "compact" (last 100 days) or "full" (20+ years)

    Returns:
        List of OHLCV dicts sorted ascending by date.

    Raises:
        ValueError:  If the API returns no data or an error message.
        RuntimeError: If all retries are exhausted.
    """
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": outputsize,
        "apikey": api_key,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info({"action": "api_fetch", "symbol": symbol, "attempt": attempt})
            response = requests.get(BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            raw = response.json()
            records = _parse_daily_series(symbol, raw)
            logger.info({"action": "api_fetch_ok", "symbol": symbol, "records": len(records)})
            return records

        except Exception as exc:
            logger.warning({"action": "api_fetch_error", "symbol": symbol,
                            "attempt": attempt, "error": str(exc)})
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Failed to fetch {symbol} after {MAX_RETRIES} attempts: {exc}"
                ) from exc
            sleep = BACKOFF_BASE ** attempt
            logger.info({"action": "retry_backoff", "seconds": sleep})
            time.sleep(sleep)