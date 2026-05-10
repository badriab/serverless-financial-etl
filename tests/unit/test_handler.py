"""
Unit tests for lambdas/transformer/handler.py

Uses moto to mock AWS S3 — no real AWS credentials needed.

Run with:
    pytest tests/unit/test_handler.py -v
"""

import io
import json
import os

import boto3
import pandas as pd
import pytest

# Set dummy env vars BEFORE importing handler (Lambda reads them at import time)
os.environ["RAW_BUCKET"] = "test-raw-bucket"
os.environ["PROCESSED_BUCKET"] = "test-processed-bucket"
os.environ["AWS_REGION"] = "ap-south-1"
os.environ["AWS_DEFAULT_REGION"] = "ap-south-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

from moto import mock_aws

from handler import handler, _build_processed_key, _read_raw_json_from_s3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import pandas as _pd
_dates = _pd.date_range(start="2025-01-01", periods=35, freq="B")
_dates = _pd.date_range(start="2025-01-01", periods=35, freq="B")
SAMPLE_RECORDS = [
    {
        "symbol": "AAPL",
        "date": d.strftime("%Y-%m-%d"),
        "open": round(150.0 + i * 0.3, 2),
        "high": round(152.0 + i * 0.3, 2),
        "low": round(149.0 + i * 0.3, 2),
        "close": round(151.0 + i * 0.5, 2),
        "volume": 50_000_000,
    }
    for i, d in enumerate(_dates)
]


def _create_buckets(s3_client):
    """Creates the raw and processed S3 buckets in the mocked environment."""
    for bucket in ["test-raw-bucket", "test-processed-bucket"]:
        s3_client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )


def _upload_raw_json(s3_client, records: list, key: str = "raw/year=2025/month=01/day=01/data.json"):
    """Uploads a list of OHLCV records as JSON to the raw S3 bucket."""
    s3_client.put_object(
        Bucket="test-raw-bucket",
        Key=key,
        Body=json.dumps(records).encode("utf-8"),
        ContentType="application/json",
    )
    return key


# ---------------------------------------------------------------------------
# Tests: _build_processed_key
# ---------------------------------------------------------------------------

class TestBuildProcessedKey:

    def test_replaces_raw_prefix(self):
        key = _build_processed_key("raw/year=2025/month=05/day=08/data.json")
        assert key.startswith("processed/")

    def test_replaces_json_extension_with_parquet(self):
        key = _build_processed_key("raw/year=2025/month=05/day=08/data.json")
        assert key.endswith(".parquet")

    def test_preserves_date_partition(self):
        key = _build_processed_key("raw/year=2025/month=05/day=08/data.json")
        assert "year=2025/month=05/day=08" in key

    def test_full_key_structure(self):
        raw = "raw/year=2025/month=05/day=08/data.json"
        expected = "processed/year=2025/month=05/day=08/data.parquet"
        assert _build_processed_key(raw) == expected


# ---------------------------------------------------------------------------
# Tests: handler (full integration with mocked S3)
# ---------------------------------------------------------------------------

class TestHandler:

    @mock_aws
    def test_handler_returns_ok_status(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        assert result["status"] == "ok"

    @mock_aws
    def test_handler_returns_correct_row_count(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        assert result["rows_processed"] == len(SAMPLE_RECORDS)

    @mock_aws
    def test_handler_writes_parquet_to_processed_bucket(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        # Verify the Parquet file actually exists in the processed bucket
        response = s3.get_object(
            Bucket="test-processed-bucket", Key=result["processed_key"]
        )
        parquet_bytes = response["Body"].read()
        assert len(parquet_bytes) > 0

    @mock_aws
    def test_handler_parquet_is_readable_dataframe(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        # Read back the Parquet and verify it's a valid DataFrame
        response = s3.get_object(
            Bucket="test-processed-bucket", Key=result["processed_key"]
        )
        df = pd.read_parquet(io.BytesIO(response["Body"].read()))
        assert len(df) == len(SAMPLE_RECORDS)
        assert "sma_7" in df.columns
        assert "anomaly_flag" in df.columns

    @mock_aws
    def test_handler_result_contains_symbols(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        assert "AAPL" in result["symbols"]

    @mock_aws
    def test_handler_result_contains_elapsed_ms(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, SAMPLE_RECORDS)

        result = handler({"raw_key": raw_key}, context=None)

        assert "elapsed_ms" in result
        assert result["elapsed_ms"] >= 0

    @mock_aws
    def test_handler_raises_on_missing_raw_key_in_event(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)

        with pytest.raises(ValueError, match="raw_key"):
            handler({}, context=None)

    @mock_aws
    def test_handler_raises_on_empty_json_array(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)
        raw_key = _upload_raw_json(s3, [])  # empty list

        with pytest.raises(ValueError):
            handler({"raw_key": raw_key}, context=None)

    @mock_aws
    def test_handler_raises_on_missing_env_vars(self):
        # Temporarily remove env vars
        raw = os.environ.pop("RAW_BUCKET", None)
        processed = os.environ.pop("PROCESSED_BUCKET", None)
        try:
            with pytest.raises(EnvironmentError, match="RAW_BUCKET"):
                handler({"raw_key": "raw/test.json"}, context=None)
        finally:
            if raw:
                os.environ["RAW_BUCKET"] = raw
            if processed:
                os.environ["PROCESSED_BUCKET"] = processed

    @mock_aws
    def test_handler_raises_on_nonexistent_s3_key(self):
        s3 = boto3.client("s3", region_name="ap-south-1")
        _create_buckets(s3)

        with pytest.raises(Exception):  # S3 NoSuchKey
            handler({"raw_key": "raw/does-not-exist.json"}, context=None)