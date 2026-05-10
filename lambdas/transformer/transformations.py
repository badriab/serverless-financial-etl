import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Optional


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validates and cleans raw OHLCV data.
    - Drops rows with nulls in critical columns
    - Ensures correct data types
    - Removes duplicate (symbol, date) pairs
    - Removes rows where high < low (data corruption)
    """
    required_cols = {"symbol", "date", "open", "high", "low", "close", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = df.dropna(subset=["symbol", "date", "close", "volume"])

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"])

    df = df[df["high"] >= df["low"]]
    df = df[df["close"] > 0]
    df = df[df["volume"] >= 0]

    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

    return df


def compute_simple_moving_average(
    df: pd.DataFrame, window: int, col: str = "close"
) -> pd.Series:
    """
    Computes simple moving average for a given window size.
    Groups by symbol so averages don't bleed across tickers.
    Returns NaN for rows with insufficient history.
    """
    return (
        df.groupby("symbol")[col]
        .transform(lambda x: x.rolling(window=window, min_periods=window).mean())
        .round(4)
    )


def compute_pct_change(df: pd.DataFrame, col: str = "close") -> pd.Series:
    """
    Computes day-over-day percentage change in closing price.
    Grouped by symbol. Returns NaN for first row of each symbol.
    """
    return (
        df.groupby("symbol")[col]
        .transform(lambda x: x.pct_change() * 100)
        .round(4)
    )


def compute_daily_range(df: pd.DataFrame) -> pd.Series:
    """
    Computes the intraday price range: high - low.
    Useful as a volatility proxy.
    """
    return (df["high"] - df["low"]).round(4)


def compute_volume_zscore(df: pd.DataFrame, window: int = 30) -> pd.Series:
    """
    Computes rolling z-score of volume over a given window.
    Z-score = (today_volume - rolling_mean) / rolling_std
    Values > 2 or < -2 indicate unusual trading activity.
    Groups by symbol.
    """
    def rolling_zscore(series: pd.Series) -> pd.Series:
        rolling_mean = series.rolling(window=window, min_periods=2).mean()
        rolling_std = series.rolling(window=window, min_periods=2).std()
        zscore = (series - rolling_mean) / rolling_std
        return zscore.replace([np.inf, -np.inf], np.nan)

    return (
        df.groupby("symbol")["volume"]
        .transform(rolling_zscore)
        .round(4)
    )


def flag_anomalies(
    df: pd.DataFrame,
    pct_change_threshold: float = 10.0,
    volume_zscore_threshold: float = 2.5,
) -> pd.Series:
    """
    Flags rows as anomalous if EITHER condition is true:
    - Absolute daily price change exceeds threshold (default 10%)
    - Volume z-score exceeds threshold (default 2.5 std deviations)

    These rows warrant manual review before use in analytics.
    """
    price_anomaly = df["pct_change_1d"].abs() > pct_change_threshold
    volume_anomaly = df["volume_zscore"].abs() > volume_zscore_threshold

    price_flag = price_anomaly.fillna(False)
    volume_flag = volume_anomaly.fillna(False)

    return price_flag | volume_flag


def add_processed_timestamp(df: pd.DataFrame) -> pd.Series:
    """Returns a UTC timestamp Series for when this batch was processed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return pd.Series([now] * len(df), index=df.index)


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """
    Master transformation function. Applies the full pipeline:
    1. Clean and validate raw OHLCV data
    2. Compute SMA-7 and SMA-30
    3. Compute day-over-day percentage change
    4. Compute daily price range
    5. Compute volume z-score (30-day rolling)
    6. Flag anomalous rows
    7. Add processed timestamp

    Args:
        df: Raw OHLCV DataFrame with columns:
            symbol, date, open, high, low, close, volume

    Returns:
        Transformed DataFrame with additional derived columns.

    Raises:
        ValueError: If required columns are missing or df is empty after cleaning.
    """
    if df.empty:
        raise ValueError("Input DataFrame is empty.")

    df = clean_ohlcv(df)

    if df.empty:
        raise ValueError("DataFrame is empty after cleaning — check input data quality.")

    df["sma_7"] = compute_simple_moving_average(df, window=7)
    df["sma_30"] = compute_simple_moving_average(df, window=30)
    df["pct_change_1d"] = compute_pct_change(df)
    df["daily_range"] = compute_daily_range(df)
    df["volume_zscore"] = compute_volume_zscore(df)
    df["anomaly_flag"] = flag_anomalies(df)
    df["processed_at"] = add_processed_timestamp(df)

    output_cols = [
        "symbol", "date", "open", "high", "low", "close", "volume",
        "sma_7", "sma_30", "pct_change_1d", "daily_range",
        "volume_zscore", "anomaly_flag", "processed_at",
    ]
    return df[output_cols].reset_index(drop=True)