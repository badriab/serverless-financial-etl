# serverless-financial-etl

> A production-grade, serverless ETL pipeline on AWS that ingests daily financial market data, transforms it with business-grade logic, and loads it into a relational database — fully automated, infrastructure-as-code, with CI/CD.

[![CI](https://github.com/badriab/serverless-financial-etl/actions/workflows/ci.yml/badge.svg)](https://github.com/badriab/serverless-financial-etl/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
![AWS CDK](https://img.shields.io/badge/IaC-AWS_CDK-orange)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Overview

This pipeline automatically ingests OHLCV (Open, High, Low, Close, Volume) equity data from a public financial API, applies transformation logic (rolling averages, percentage changes, anomaly flags), and loads clean records into a PostgreSQL database on Amazon RDS — all on a daily schedule with zero manual intervention.

**Use case:** Financial analytics teams and fintech startups that need reliable, clean market data snapshots delivered to a queryable database every day — without maintaining a server.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        AWS Cloud                            │
│                                                             │
│  EventBridge (daily cron)                                   │
│       │                                                     │
│       ▼                                                     │
│  Lambda: ingestor.py ──► S3 (raw JSON)                      │
│       │                                                     │
│       ▼                                                     │
│  Lambda: transformer.py ──► S3 (processed Parquet)          │
│       │                                                     │
│       ▼                                                     │
│  Lambda: loader.py ──► RDS PostgreSQL (equity_snapshots)    │
│                                                             │
│  CloudWatch Logs ◄── all Lambdas                            │
│  SNS ◄── Lambda error alerts                                │
└─────────────────────────────────────────────────────────────┘
```

> Architecture diagram (full): [`docs/architecture.png`](docs/architecture.png)

| Component | Service | Purpose |
|---|---|---|
| Scheduler | Amazon EventBridge | Triggers pipeline daily at 06:00 UTC |
| Ingestion | AWS Lambda (Python 3.11) | Fetches raw OHLCV data from Alpha Vantage API |
| Raw storage | Amazon S3 | Stores raw JSON with date-partitioned prefix |
| Transformation | AWS Lambda (Python 3.11) | Cleans, normalises, computes rolling metrics |
| Processed storage | Amazon S3 | Stores transformed Parquet files |
| Loading | AWS Lambda (Python 3.11) | Upserts clean records into RDS PostgreSQL |
| Database | Amazon RDS (PostgreSQL 15) | Queryable store for downstream analytics |
| Alerting | Amazon SNS | Email alert on any Lambda failure |
| IaC | AWS CDK (Python) | Full stack defined and deployable in one command |

---

## Tech Stack

- **Runtime:** Python 3.11
- **Libraries:** `pandas`, `boto3`, `psycopg2-binary`, `requests`, `pyarrow`
- **Cloud:** AWS Lambda, S3, RDS, EventBridge, SNS, CloudWatch, Secrets Manager
- **IaC:** AWS CDK (Python)
- **CI/CD:** GitHub Actions (lint → test → `cdk synth`)
- **Testing:** `pytest`, `pytest-cov`, `moto` (AWS mocking)
- **Code quality:** `black`, `flake8`, `isort`

---

## Project Structure

```
serverless-financial-etl/
├── lambdas/
│   ├── ingestor/
│   │   ├── handler.py          # Lambda entry point: fetch from Alpha Vantage API
│   │   ├── api_client.py       # HTTP client with retry logic and error handling
│   │   └── requirements.txt
│   ├── transformer/
│   │   ├── handler.py          # Lambda entry point: pandas transformation logic
│   │   ├── transformations.py  # Rolling averages, pct change, anomaly flags
│   │   └── requirements.txt
│   └── loader/
│       ├── handler.py          # Lambda entry point: upsert into RDS PostgreSQL
│       ├── db_client.py        # Connection pooling, upsert logic, error handling
│       └── requirements.txt
├── infrastructure/
│   ├── app.py                  # CDK app entry point
│   ├── etl_stack.py            # Full stack definition (all AWS resources)
│   └── requirements.txt
├── tests/
│   ├── unit/
│   │   ├── test_transformer.py # 12 unit tests for transformation logic
│   │   ├── test_ingestor.py    # 6 unit tests with mocked API responses
│   │   └── test_loader.py      # 8 unit tests with mocked DB + moto S3
│   └── conftest.py             # Shared fixtures
├── docs/
│   └── architecture.png        # Architecture diagram
├── sql/
│   └── schema.sql              # Table definitions + indexes
├── .github/
│   └── workflows/
│       └── ci.yml              # Lint → test → cdk synth on every push
├── .env.example                # Required environment variables (no secrets)
├── Makefile                    # Common dev commands
└── README.md
```

---

## Key Features

- **Zero-server architecture** — no EC2 instances to manage or patch
- **Idempotent upserts** — re-running the pipeline never creates duplicate records
- **Date-partitioned S3 storage** — `s3://bucket/raw/year=2025/month=05/day=09/`
- **Secrets Manager integration** — DB credentials never in environment variables or code
- **Dead letter queues** — failed Lambda invocations captured for inspection
- **SNS alerting** — email notification within 60 seconds of any pipeline failure
- **Full test suite** — 26 unit tests, 85%+ coverage, AWS services mocked with `moto`
- **One-command deploy** — `cdk deploy --all` provisions the entire stack from scratch

---

## Sample Data: Transformation Logic

Input (raw from API):
```json
{
  "symbol": "AAPL",
  "date": "2025-05-08",
  "open": 182.50,
  "high": 185.20,
  "low": 181.30,
  "close": 184.10,
  "volume": 62480000
}
```

Output (after transformation):
```json
{
  "symbol": "AAPL",
  "date": "2025-05-08",
  "close": 184.10,
  "sma_7": 183.42,
  "sma_30": 179.85,
  "pct_change_1d": 0.87,
  "daily_range": 3.90,
  "volume_zscore": 1.24,
  "anomaly_flag": false,
  "processed_at": "2025-05-09T06:04:31Z"
}
```

---

## Database Schema

```sql
-- sql/schema.sql
CREATE TABLE equity_snapshots (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR(10)    NOT NULL,
    date          DATE           NOT NULL,
    close         NUMERIC(10,4)  NOT NULL,
    sma_7         NUMERIC(10,4),
    sma_30        NUMERIC(10,4),
    pct_change_1d NUMERIC(8,4),
    daily_range   NUMERIC(10,4),
    volume_zscore NUMERIC(8,4),
    anomaly_flag  BOOLEAN        DEFAULT FALSE,
    processed_at  TIMESTAMPTZ    DEFAULT NOW(),
    UNIQUE (symbol, date)
);

CREATE INDEX idx_equity_symbol_date ON equity_snapshots (symbol, date DESC);
```

---

## AWS Cost Estimate

This stack runs within the AWS Free Tier for personal/portfolio use.

| Service | Free Tier | Estimated monthly (production) |
|---|---|---|
| Lambda | 1M requests/month free | < $0.50 |
| S3 | 5 GB free | < $0.10 |
| RDS PostgreSQL (db.t3.micro) | 750 hrs/month free (12 mo) | ~$13–15 |
| EventBridge | 14M events/month free | $0.00 |
| CloudWatch Logs | 5 GB ingestion free | < $0.50 |
| SNS | 1M notifications free | $0.00 |
| **Total** | **~$0 (Free Tier)** | **~$15/month** |

> Cost calculated for a 5-symbol, daily-run pipeline. Scales linearly with symbol count.

---

## Getting Started

### Prerequisites

- AWS account with CLI configured (`aws configure`)
- Python 3.11+
- Node.js 18+ (required for AWS CDK CLI)
- AWS CDK CLI: `npm install -g aws-cdk`
- Alpha Vantage API key (free at [alphavantage.co](https://www.alphavantage.co))

### 1. Clone and install

```bash
git clone https://github.com/badriab/serverless-financial-etl.git
cd serverless-financial-etl

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r infrastructure/requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add your Alpha Vantage API key and target symbols
```

`.env.example`:
```
ALPHA_VANTAGE_API_KEY=your_key_here
TARGET_SYMBOLS=AAPL,MSFT,GOOGL,AMZN,TSLA
DB_NAME=financial_etl
AWS_REGION=ap-south-1
ALERT_EMAIL=your@email.com
```

### 3. Bootstrap CDK (first time only)

```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/ap-south-1
```

### 4. Deploy the full stack

```bash
cdk deploy --all
```

This single command provisions: S3 buckets, Lambda functions, RDS instance, EventBridge rule, SNS topic, IAM roles, CloudWatch log groups, and Secrets Manager entries.

> Expected deploy time: 8–12 minutes (RDS provisioning dominates)

### 5. Verify deployment

```bash
# Manually trigger the pipeline
aws lambda invoke \
  --function-name etl-ingestor \
  --payload '{}' \
  response.json

cat response.json
```

---

## Running Tests

```bash
# All tests with coverage report
make test

# Or manually:
pip install -r tests/requirements.txt
pytest tests/ -v --cov=lambdas --cov-report=term-missing
```

Expected output:
```
tests/unit/test_transformer.py::test_sma_calculation PASSED
tests/unit/test_transformer.py::test_anomaly_flag_triggered PASSED
tests/unit/test_transformer.py::test_pct_change_accuracy PASSED
...
---------- coverage: 87% ----------
26 passed in 4.31s
```

---

## CI/CD Pipeline

Every push to `main` or any pull request triggers the GitHub Actions workflow:

```
Push / PR
    │
    ├─► Lint: black --check, flake8, isort --check
    │
    ├─► Test: pytest (26 tests, coverage threshold 80%)
    │
    └─► CDK Synth: cdk synth (validates CloudFormation output)
```

Workflow file: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)

Deployments to AWS are intentionally manual (`cdk deploy`) — this is appropriate for a portfolio project and mirrors real-world change-approval processes.

---

## Local Development

```bash
make lint       # Run black + flake8 + isort
make test       # Run pytest with coverage
make synth      # Run cdk synth (dry-run CloudFormation)
make clean      # Remove __pycache__ and .pytest_cache
```

To test a Lambda locally without AWS:
```bash
cd lambdas/transformer
python -c "from handler import handler; handler({'key': 'raw/2025/05/09/data.json'}, {})"
```

---

## Skills Demonstrated

This project was built to demonstrate the following for freelance engagements:

| Skill | Where demonstrated |
|---|---|
| Python data engineering | `transformations.py` — pandas, rolling windows, z-scores |
| AWS serverless architecture | Lambda → S3 → RDS event-driven pipeline |
| Infrastructure as Code | Full CDK stack in `etl_stack.py` |
| CI/CD | GitHub Actions workflow with lint, test, synth gates |
| Production practices | Upserts, DLQs, Secrets Manager, SNS alerts |
| Testing discipline | 26 unit tests, moto mocks, 87% coverage |
| Cost awareness | Free Tier compatible, cost breakdown documented |

---

## Author

**Aliasgar Badri** — Freelance Python & AWS Engineer

- AWS Certified Solutions Architect – Associate
- AWS Certified Developer – Associate
- GitHub Copilot Certified
- 4+ years building Python data pipelines and cloud infrastructure

Open to freelance engagements in Python automation, AWS data engineering, and Flask API development.

[LinkedIn](https://www.linkedin.com/in/aliasgar-badri-64941614b) · [GitHub](https://github.com/badriab) · [Email](mailto:aliasgarbadri5352@gmail.com)

---

## License

MIT — free to use as a reference or starting point.
