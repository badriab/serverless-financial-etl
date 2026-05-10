"""
Unit tests for lambdas/transformer/transformations.py

Run with:
    pytest tests/unit/test_transformer.py -v --cov=lambdas/transformer --cov-report=term-missing
"""

import math
import pytest
import pandas as pd
import numpy as np
from datetime import date, datetime, timezone

from transformations import (
    clean_ohlcv,
    compute_simple_moving_average,
    compute_pct_change,
    compute_daily_range,
    compute_volume_zscore,
    flag_anomalies,
    transform,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_ohlcv(
    symbol="AAPL",
    n=35,
    base_close=150.0,
    base_volume=50_000_000,
) -> pd.DataFrame:
    """
    Generates n days of synthetic OHLCV data for one symbol.
    Close price walks +0.5 per day. Volume is constant by default.
    """
    dates = pd.date_range(start="2025-01-01", periods=n, freq="B")  # business days
    closes = [round(base_close + i * 0.5, 2) for i in range(n)]
    rows = []
    for i, d in enumerate(dates):
        close = closes[i]
        rows.append({
            "symbol": symbol,
            "date": d.strftime("%Y-%m-%d"),
            "open": round(close - 0.3, 2),
            "high": round(close + 1.0, 2),
            "low": round(close - 1.0, 2),
            "close": close,
            "volume": base_volume,
        })
    return pd.DataFrame(rows)


def make_multi_symbol(symbols=("AAPL", "MSFT"), n=35) -> pd.DataFrame:
    """Generates synthetic data for multiple symbols."""
    frames = [make_ohlcv(symbol=s, n=n, base_close=100.0 + i * 50) for i, s in enumerate(symbols)]
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# 1. clean_ohlcv
# ---------------------------------------------------------------------------

class TestCleanOhlcv:

    def test_returns_dataframe(self):
        df = make_ohlcv(n=5)
        result = clean_ohlcv(df)
        assert isinstance(result, pd.DataFrame)

    def test_drops_null_close(self):
        df = make_ohlcv(n=5)
        df.loc[2, "close"] = None
        result = clean_ohlcv(df)
        assert len(result) == 4

    def test_drops_null_symbol(self):
        df = make_ohlcv(n=5)
        df.loc[0, "symbol"] = None
        result = clean_ohlcv(df)
        assert len(result) == 4

    def test_drops_rows_where_high_less_than_low(self):
        df = make_ohlcv(n=5)
        df.loc[1, "high"] = df.loc[1, "low"] - 1.0  # corrupt row
        result = clean_ohlcv(df)
        assert len(result) == 4

    def test_drops_negative_close(self):
        df = make_ohlcv(n=5)
        df.loc[3, "close"] = -5.0
        result = clean_ohlcv(df)
        assert len(result) == 4

    def test_deduplicates_symbol_date(self):
        df = make_ohlcv(n=5)
        duplicate = df.iloc[2:3].copy()
        df = pd.concat([df, duplicate], ignore_index=True)
        result = clean_ohlcv(df)
        assert len(result) == 5

    def test_raises_on_missing_required_columns(self):
        df = make_ohlcv(n=3).drop(columns=["volume"])
        with pytest.raises(ValueError, match="Missing required columns"):
            clean_ohlcv(df)

    def test_date_column_is_converted(self):
        df = make_ohlcv(n=3)
        result = clean_ohlcv(df)
        assert all(isinstance(d, date) for d in result["date"])

    def test_sorts_by_symbol_then_date(self):
        df = make_multi_symbol(n=5)
        df = df.sample(frac=1, random_state=42)  # shuffle
        result = clean_ohlcv(df)
        for symbol, group in result.groupby("symbol"):
            dates = list(group["date"])
            assert dates == sorted(dates), f"{symbol} dates not sorted"


# ---------------------------------------------------------------------------
# 2. compute_simple_moving_average
# ---------------------------------------------------------------------------

class TestSMA:

    def test_sma7_first_six_rows_are_nan(self):
        df = clean_ohlcv(make_ohlcv(n=20))
        sma = compute_simple_moving_average(df, window=7)
        assert sma.iloc[:6].isna().all()

    def test_sma7_seventh_row_is_not_nan(self):
        df = clean_ohlcv(make_ohlcv(n=20))
        sma = compute_simple_moving_average(df, window=7)
        assert not pd.isna(sma.iloc[6])

    def test_sma7_value_correctness(self):
        df = clean_ohlcv(make_ohlcv(n=10, base_close=100.0))
        sma = compute_simple_moving_average(df, window=7)
        # Manually compute: first 7 closes = 100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0
        expected = round(sum([100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0]) / 7, 4)
        assert abs(sma.iloc[6] - expected) < 0.001

    def test_sma_does_not_bleed_across_symbols(self):
        df = clean_ohlcv(make_multi_symbol(symbols=("AAPL", "MSFT"), n=35))
        sma = compute_simple_moving_average(df, window=7)
        # The 7th row of MSFT should not be influenced by AAPL's closing prices
        msft_rows = df[df["symbol"] == "MSFT"].copy()
        msft_rows["sma"] = sma[df["symbol"] == "MSFT"].values
        # If bleeding occurred, the SMA would incorporate AAPL closes (different base)
        aapl_close = df[df["symbol"] == "AAPL"]["close"].mean()
        msft_close = df[df["symbol"] == "MSFT"]["close"].mean()
        msft_sma_mean = msft_rows["sma"].dropna().mean()
        assert abs(msft_sma_mean - msft_close) < abs(msft_sma_mean - aapl_close)


# ---------------------------------------------------------------------------
# 3. compute_pct_change
# ---------------------------------------------------------------------------

class TestPctChange:

    def test_first_row_per_symbol_is_nan(self):
        df = clean_ohlcv(make_ohlcv(n=5))
        pct = compute_pct_change(df)
        assert pd.isna(pct.iloc[0])

    def test_pct_change_accuracy(self):
        df = clean_ohlcv(make_ohlcv(n=3, base_close=100.0))
        pct = compute_pct_change(df)
        # Close goes 100.0 → 100.5: pct change = (100.5 - 100.0) / 100.0 * 100 = 0.5
        assert abs(pct.iloc[1] - 0.5) < 0.01

    def test_pct_change_negative_on_price_drop(self):
        df = pd.DataFrame([
            {"symbol": "TEST", "date": "2025-01-01", "open": 100, "high": 105,
             "low": 95, "close": 100.0, "volume": 1_000_000},
            {"symbol": "TEST", "date": "2025-01-02", "open": 98, "high": 102,
             "low": 90, "close": 90.0, "volume": 1_000_000},
        ])
        df = clean_ohlcv(df)
        pct = compute_pct_change(df)
        assert pct.iloc[1] < 0

    def test_pct_change_does_not_bleed_across_symbols(self):
        df = clean_ohlcv(make_multi_symbol(n=5))
        pct = compute_pct_change(df)
        first_msft_idx = df[df["symbol"] == "MSFT"].index[0]
        assert pd.isna(pct.iloc[first_msft_idx])


# ---------------------------------------------------------------------------
# 4. compute_daily_range
# ---------------------------------------------------------------------------

class TestDailyRange:

    def test_range_equals_high_minus_low(self):
        df = clean_ohlcv(make_ohlcv(n=5))
        dr = compute_daily_range(df)
        for i in range(len(df)):
            expected = round(df.iloc[i]["high"] - df.iloc[i]["low"], 4)
            assert abs(dr.iloc[i] - expected) < 0.0001

    def test_range_is_always_non_negative(self):
        df = clean_ohlcv(make_ohlcv(n=20))
        dr = compute_daily_range(df)
        assert (dr >= 0).all()


# ---------------------------------------------------------------------------
# 5. flag_anomalies
# ---------------------------------------------------------------------------

class TestFlagAnomalies:

    def _make_flaggable_df(self):
        """Returns a transformed df with manually set pct_change_1d and volume_zscore."""
        df = clean_ohlcv(make_ohlcv(n=35))
        df["sma_7"] = compute_simple_moving_average(df, window=7)
        df["sma_30"] = compute_simple_moving_average(df, window=30)
        df["pct_change_1d"] = compute_pct_change(df)
        df["daily_range"] = compute_daily_range(df)
        df["volume_zscore"] = compute_volume_zscore(df)
        return df

    def test_large_price_move_is_flagged(self):
        df = self._make_flaggable_df()
        df.loc[df.index[20], "pct_change_1d"] = 15.0  # > 10% threshold
        flags = flag_anomalies(df)
        assert flags.iloc[20] is True or flags.iloc[20] == True

    def test_large_volume_zscore_is_flagged(self):
        df = self._make_flaggable_df()
        df.loc[df.index[20], "volume_zscore"] = 3.5  # > 2.5 threshold
        flags = flag_anomalies(df)
        assert flags.iloc[20] is True or flags.iloc[20] == True

    def test_normal_rows_are_not_flagged(self):
        df = self._make_flaggable_df()
        df["pct_change_1d"] = 0.3   # small moves
        df["volume_zscore"] = 0.5   # normal volume
        flags = flag_anomalies(df)
        assert not flags.any()

    def test_custom_threshold_respected(self):
        df = self._make_flaggable_df()
        df["pct_change_1d"] = 8.0   # above 5% custom threshold, below 10% default
        df["volume_zscore"] = 0.0
        flags = flag_anomalies(df, pct_change_threshold=5.0)
        assert flags.any()


# ---------------------------------------------------------------------------
# 6. transform (master function)
# ---------------------------------------------------------------------------

class TestTransform:

    def test_returns_dataframe(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        assert isinstance(result, pd.DataFrame)

    def test_output_has_all_expected_columns(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        expected_cols = {
            "symbol", "date", "open", "high", "low", "close", "volume",
            "sma_7", "sma_30", "pct_change_1d", "daily_range",
            "volume_zscore", "anomaly_flag", "processed_at",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_raises_on_empty_dataframe(self):
        with pytest.raises(ValueError, match="empty"):
            transform(pd.DataFrame())

    def test_raises_after_cleaning_yields_empty(self):
        df = make_ohlcv(n=3)
        df["close"] = None  # all rows will be dropped by clean_ohlcv
        with pytest.raises(ValueError):
            transform(df)

    def test_processed_at_is_utc_iso_format(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        ts = result["processed_at"].iloc[0]
        assert ts.endswith("Z"), f"Expected UTC ISO format ending in Z, got: {ts}"
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")  # raises if format is wrong

    def test_anomaly_flag_column_is_boolean(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        assert result["anomaly_flag"].dtype == bool or result["anomaly_flag"].isin([True, False]).all()

    def test_sma_30_requires_30_rows_of_history(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        aapl = result[result["symbol"] == "AAPL"]
        # First 29 rows should have NaN sma_30
        assert aapl["sma_30"].iloc[:29].isna().all()
        # Row 30 (index 29) should be populated
        assert not pd.isna(aapl["sma_30"].iloc[29])

    def test_row_count_preserved_after_transform(self):
        df = make_ohlcv(n=35)
        result = transform(df)
        assert len(result) == 35

    def test_multi_symbol_transform(self):
        df = make_multi_symbol(symbols=("AAPL", "MSFT", "GOOGL"), n=35)
        result = transform(df)
        assert set(result["symbol"].unique()) == {"AAPL", "MSFT", "GOOGL"}
        assert len(result) == 35 * 3