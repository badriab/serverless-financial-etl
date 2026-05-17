"""
Unit tests for lambdas/loader/db_client.py and handler.py
Run: pytest tests/unit/test_loader.py -v
"""

import importlib.util, io, json, os, sys
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest
import boto3
from moto import mock_aws

# --------------------------------------------------------------------------
# Dummy AWS env vars
# --------------------------------------------------------------------------
os.environ.update({
    "PROCESSED_BUCKET":   "test-processed-bucket",
    "DB_SECRET_NAME":     "etl/test-db-secret",
    "AWS_REGION":         "ap-south-1",
    "AWS_DEFAULT_REGION": "ap-south-1",
    "AWS_ACCESS_KEY_ID":  "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN":  "testing",
})

# --------------------------------------------------------------------------
# Explicit module loading — avoids handler name collision
# --------------------------------------------------------------------------
_loader_dir = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../lambdas/loader")
)
if _loader_dir not in sys.path:
    sys.path.insert(0, _loader_dir)

def _load(reg_name, filepath):
    spec = importlib.util.spec_from_file_location(reg_name, filepath)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[reg_name] = mod          # register so @patch can find it
    spec.loader.exec_module(mod)
    return mod

db_mod      = _load("loader_db_client",  os.path.join(_loader_dir, "db_client.py"))
handler_mod = _load("loader_handler",    os.path.join(_loader_dir, "handler.py"))

upsert_records = db_mod.upsert_records
handler        = handler_mod.handler
_df_to_records = handler_mod._df_to_records
_safe_float    = handler_mod._safe_float

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
import pandas as _pd
_DATES = _pd.date_range(start="2025-01-01", periods=35, freq="B")

def _make_df():
    rows = []
    for i, d in enumerate(_DATES):
        close = round(151.0 + i * 0.5, 2)
        rows.append({
            "symbol": "AAPL", "date": d.date(),
            "open": close - 0.3, "high": close + 1.0,
            "low": close - 1.0,  "close": close,
            "volume": 50_000_000.0,
            "sma_7":        round(close - 1.5, 4) if i >= 6  else float("nan"),
            "sma_30":       round(close - 3.0, 4) if i >= 29 else float("nan"),
            "pct_change_1d":0.33 if i > 0 else float("nan"),
            "daily_range":  2.0,
            "volume_zscore":0.5  if i >= 2 else float("nan"),
            "anomaly_flag": False,
            "processed_at": "2025-05-09T06:04:31Z",
        })
    return pd.DataFrame(rows)

def _to_parquet(df):
    buf = io.BytesIO(); df.to_parquet(buf, index=False); return buf.getvalue()

def _make_bucket(s3):
    s3.create_bucket(Bucket="test-processed-bucket",
        CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})

def _upload(s3, df, key="processed/year=2025/month=01/day=01/data.parquet"):
    s3.put_object(Bucket="test-processed-bucket", Key=key, Body=_to_parquet(df))
    return key

# ==========================================================================
# _safe_float
# ==========================================================================
class TestSafeFloat:
    def test_normal_float(self):     assert _safe_float(3.14) == 3.14
    def test_nan_returns_none(self): assert _safe_float(float("nan")) is None
    def test_none_returns_none(self):assert _safe_float(None) is None
    def test_int_converted(self):    assert _safe_float(5) == 5.0
    def test_string_number(self):    assert _safe_float("2.5") == 2.5

# ==========================================================================
# _df_to_records
# ==========================================================================
class TestDfToRecords:
    def test_returns_list(self):
        assert isinstance(_df_to_records(_make_df()), list)
    def test_length(self):
        assert len(_df_to_records(_make_df())) == 35
    def test_required_keys(self):
        required = {"symbol","date","open","high","low","close","volume",
                    "sma_7","sma_30","pct_change_1d","daily_range",
                    "volume_zscore","anomaly_flag","processed_at"}
        for r in _df_to_records(_make_df()):
            assert required.issubset(r.keys())
    def test_nan_sma_is_none(self):
        assert _df_to_records(_make_df())[0]["sma_7"] is None
    def test_anomaly_flag_bool(self):
        assert all(isinstance(r["anomaly_flag"], bool) for r in _df_to_records(_make_df()))
    def test_date_is_string(self):
        assert all(isinstance(r["date"], str) for r in _df_to_records(_make_df()))

# ==========================================================================
# upsert_records  (mock psycopg2 at the db_client module level)
# ==========================================================================
class TestUpsertRecords:
    def _mock_conn(self, rowcount=5):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__  = MagicMock(return_value=False)
        cur.rowcount  = rowcount
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    @patch("loader_db_client.psycopg2.extras.execute_batch")
    def test_calls_execute_batch(self, mock_eb):
        conn, cur = self._mock_conn(5)
        records = _df_to_records(_make_df().head(5))
        result = upsert_records(conn, records)
        mock_eb.assert_called_once()
        assert result == 5

    @patch("loader_db_client.psycopg2.extras.execute_batch")
    def test_empty_records_returns_zero(self, mock_eb):
        conn, _ = self._mock_conn()
        assert upsert_records(conn, []) == 0
        mock_eb.assert_not_called()

    @patch("loader_db_client.psycopg2.extras.execute_batch")
    def test_nan_replaced_with_none(self, mock_eb):
        conn, cur = self._mock_conn(1)
        records = [{"sma_7": float("nan"), "symbol":"AAPL","date":"2025-01-01",
                    "open":150.0,"high":152.0,"low":149.0,"close":151.0,"volume":1e7,
                    "sma_30":float("nan"),"pct_change_1d":float("nan"),"daily_range":3.0,
                    "volume_zscore":float("nan"),"anomaly_flag":False,
                    "processed_at":"2025-05-09T06:00:00Z"}]
        upsert_records(conn, records)
        passed = mock_eb.call_args[0][2]
        assert passed[0]["sma_7"] is None

# ==========================================================================
# handler (mocked S3 + mocked db_client functions)
# ==========================================================================
class TestLoaderHandler:
    def _mock_ctx(self, mock_conn_ctx, mock_upsert, rows=35):
        conn = MagicMock()
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn_ctx.return_value.__exit__  = MagicMock(return_value=False)
        mock_upsert.return_value = rows
        return conn

    @mock_aws
    @patch("loader_handler.get_connection")
    @patch("loader_handler.upsert_records")
    def test_returns_ok(self, mu, mc):
        s3 = boto3.client("s3", region_name="ap-south-1"); _make_bucket(s3)
        key = _upload(s3, _make_df())
        self._mock_ctx(mc, mu)
        assert handler({"processed_key": key}, None)["status"] == "ok"

    @mock_aws
    @patch("loader_handler.get_connection")
    @patch("loader_handler.upsert_records")
    def test_correct_row_count(self, mu, mc):
        s3 = boto3.client("s3", region_name="ap-south-1"); _make_bucket(s3)
        key = _upload(s3, _make_df())
        self._mock_ctx(mc, mu, rows=35)
        assert handler({"processed_key": key}, None)["rows_upserted"] == 35

    @mock_aws
    @patch("loader_handler.get_connection")
    @patch("loader_handler.upsert_records")
    def test_calls_commit(self, mu, mc):
        s3 = boto3.client("s3", region_name="ap-south-1"); _make_bucket(s3)
        key = _upload(s3, _make_df())
        conn = self._mock_ctx(mc, mu)
        handler({"processed_key": key}, None)
        conn.commit.assert_called_once()

    @mock_aws
    @patch("loader_handler.get_connection")
    @patch("loader_handler.upsert_records")
    def test_symbols_in_result(self, mu, mc):
        s3 = boto3.client("s3", region_name="ap-south-1"); _make_bucket(s3)
        key = _upload(s3, _make_df())
        self._mock_ctx(mc, mu)
        assert "AAPL" in handler({"processed_key": key}, None)["symbols"]

    @mock_aws
    @patch("loader_handler.get_connection")
    @patch("loader_handler.upsert_records")
    def test_elapsed_ms_present(self, mu, mc):
        s3 = boto3.client("s3", region_name="ap-south-1"); _make_bucket(s3)
        key = _upload(s3, _make_df())
        self._mock_ctx(mc, mu)
        r = handler({"processed_key": key}, None)
        assert "elapsed_ms" in r and r["elapsed_ms"] >= 0

    @mock_aws
    def test_raises_missing_processed_key(self):
        with pytest.raises(ValueError, match="processed_key"):
            handler({}, None)

    @mock_aws
    def test_raises_missing_bucket_env(self):
        v = os.environ.pop("PROCESSED_BUCKET")
        try:
            with pytest.raises(EnvironmentError, match="PROCESSED_BUCKET"):
                handler({"processed_key": "x"}, None)
        finally:
            os.environ["PROCESSED_BUCKET"] = v

    @mock_aws
    def test_raises_missing_secret_env(self):
        v = os.environ.pop("DB_SECRET_NAME")
        try:
            with pytest.raises(EnvironmentError, match="DB_SECRET_NAME"):
                handler({"processed_key": "x"}, None)
        finally:
            os.environ["DB_SECRET_NAME"] = v