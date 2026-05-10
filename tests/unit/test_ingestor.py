"""
Unit tests for lambdas/ingestor/api_client.py and handler.py

Uses:
  - unittest.mock to mock HTTP calls to Alpha Vantage (no real API calls)
  - moto to mock AWS S3 and Secrets Manager

Run with:
    pytest tests/unit/test_ingestor.py -v
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# Set dummy AWS env vars before importing handler
os.environ.update({
    "RAW_BUCKET": "test-raw-bucket",
    "TARGET_SYMBOLS": "AAPL,MSFT",
    "AWS_REGION": "ap-south-1",
    "AWS_DEFAULT_REGION": "ap-south-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "ALPHA_VANTAGE_API_KEY": "demo_key",
})

from api_client import _parse_daily_series, fetch_daily_ohlcv
from ingestor_handler import _build_s3_key, handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_AV_RESPONSE = {
    "Time Series (Daily)": {
        "2025-05-08": {
            "1. open": "182.50",
            "2. high": "185.20",
            "3. low": "181.30",
            "4. close": "184.10",
            "5. volume": "62480000",
        },
        "2025-05-07": {
            "1. open": "180.00",
            "2. high": "183.00",
            "3. low": "179.50",
            "4. close": "182.30",
            "5. volume": "55000000",
        },
    }
}

MOCK_AV_ERROR_RESPONSE = {
    "Note": "Thank you for using Alpha Vantage! API call frequency exceeded."
}

MOCK_AV_EMPTY_RESPONSE = {}


def _make_mock_response(data: dict, status_code: int = 200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = data
    mock.raise_for_status.return_value = None
    return mock


def _create_bucket(s3_client):
    s3_client.create_bucket(
        Bucket="test-raw-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
    )


# ---------------------------------------------------------------------------
# Tests: _parse_daily_series
# ---------------------------------------------------------------------------

class TestParseDailySeries:

    def test_returns_list_of_dicts(self):
        records = _parse_daily_series("AAPL", MOCK_AV_RESPONSE)
        assert isinstance(records, list)
        assert len(records) == 2

    def test_record_has_all_ohlcv_fields(self):
        records = _parse_daily_series("AAPL", MOCK_AV_RESPONSE)
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        for record in records:
            assert required.issubset(set(record.keys()))

    def test_symbol_is_injected(self):
        records = _parse_daily_series("AAPL", MOCK_AV_RESPONSE)
        assert all(r["symbol"] == "AAPL" for r in records)

    def test_records_sorted_ascending_by_date(self):
        records = _parse_daily_series("AAPL", MOCK_AV_RESPONSE)
        dates = [r["date"] for r in records]
        assert dates == sorted(dates)

    def test_numeric_fields_are_floats(self):
        records = _parse_daily_series("AAPL", MOCK_AV_RESPONSE)
        for r in records:
            assert isinstance(r["close"], float)
            assert isinstance(r["volume"], float)

    def test_raises_on_api_error_response(self):
        with pytest.raises(ValueError, match="Alpha Vantage"):
            _parse_daily_series("AAPL", MOCK_AV_ERROR_RESPONSE)

    def test_raises_on_empty_response(self):
        with pytest.raises(ValueError):
            _parse_daily_series("AAPL", MOCK_AV_EMPTY_RESPONSE)


# ---------------------------------------------------------------------------
# Tests: fetch_daily_ohlcv
# ---------------------------------------------------------------------------

class TestFetchDailyOhlcv:

    @patch("api_client.requests.get")
    def test_returns_records_on_success(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        records = fetch_daily_ohlcv("AAPL", "test_key")
        assert len(records) == 2

    @patch("api_client.requests.get")
    def test_raises_runtime_error_after_all_retries(self, mock_get):
        mock_get.side_effect = Exception("Network error")
        with patch("api_client.time.sleep"):  # skip backoff delays in tests
            with pytest.raises(RuntimeError, match="Failed to fetch"):
                fetch_daily_ohlcv("AAPL", "test_key")

    @patch("api_client.requests.get")
    def test_retries_on_transient_failure_then_succeeds(self, mock_get):
        # First call fails, second succeeds
        mock_get.side_effect = [
            Exception("Timeout"),
            _make_mock_response(MOCK_AV_RESPONSE),
        ]
        with patch("api_client.time.sleep"):
            records = fetch_daily_ohlcv("AAPL", "test_key")
        assert len(records) == 2
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# Tests: _build_s3_key
# ---------------------------------------------------------------------------

class TestBuildS3Key:

    def test_key_format(self):
        dt = datetime(2025, 5, 9, 6, 0, 0, tzinfo=timezone.utc)
        key = _build_s3_key(dt)
        assert key == "raw/year=2025/month=05/day=09/data.json"

    def test_single_digit_month_zero_padded(self):
        dt = datetime(2025, 1, 3, tzinfo=timezone.utc)
        key = _build_s3_key(dt)
        assert "month=01" in key
        assert "day=03" in key

    def test_key_starts_with_raw(self):
        dt = datetime(2025, 5, 9, tzinfo=timezone.utc)
        assert _build_s3_key(dt).startswith("raw/")

    def test_key_ends_with_data_json(self):
        dt = datetime(2025, 5, 9, tzinfo=timezone.utc)
        assert _build_s3_key(dt).endswith("data.json")


# ---------------------------------------------------------------------------
# Tests: handler (full integration with mocked API + S3)
# ---------------------------------------------------------------------------

class TestIngestorHandler:

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_returns_ok(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_bucket(s3)

        result = handler({}, context=None)
        assert result["status"] == "ok"

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_writes_json_to_s3(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_bucket(s3)

        result = handler({}, context=None)

        obj = s3.get_object(Bucket="test-raw-bucket", Key=result["raw_key"])
        records = json.loads(obj["Body"].read())
        assert isinstance(records, list)
        assert len(records) > 0

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_result_contains_symbols_fetched(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_bucket(s3)

        result = handler({}, context=None)
        assert "AAPL" in result["symbols_fetched"]
        assert "MSFT" in result["symbols_fetched"]

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_accepts_event_symbol_override(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_bucket(s3)

        result = handler({"symbols": ["GOOGL"]}, context=None)
        assert result["symbols_fetched"] == ["GOOGL"]

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_accepts_run_date_override(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_AV_RESPONSE)
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_bucket(s3)

        result = handler({"run_date": "2025-01-15"}, context=None)
        assert "year=2025/month=01/day=15" in result["raw_key"]

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_records_failed_symbols_without_crashing(self, mock_get):
        # AAPL succeeds, MSFT fails
        mock_get.side_effect = [
            _make_mock_response(MOCK_AV_RESPONSE),   # AAPL
            Exception("API limit"),                   # MSFT
        ]
        with patch("api_client.time.sleep"):
            s3 = boto3.client("s3", region_name="ap-south-1")
            _create_bucket(s3)

            result = handler({}, context=None)

        assert "AAPL" in result["symbols_fetched"]
        assert "MSFT" in result["symbols_failed"]
        assert result["status"] == "ok"

    @mock_aws
    def test_handler_raises_on_missing_raw_bucket(self):
        original = os.environ.pop("RAW_BUCKET", None)
        try:
            with pytest.raises(EnvironmentError, match="RAW_BUCKET"):
                handler({}, context=None)
        finally:
            if original:
                os.environ["RAW_BUCKET"] = original

    @mock_aws
    @patch("api_client.requests.get")
    def test_handler_raises_when_all_symbols_fail(self, mock_get):
        mock_get.side_effect = Exception("Total outage")
        with patch("api_client.time.sleep"):
            s3 = boto3.client("s3", region_name="ap-south-1")
            _create_bucket(s3)

            with pytest.raises(RuntimeError, match="No data fetched"):
                handler({}, context=None)