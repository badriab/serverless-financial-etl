.PHONY: install lint test test-cov clean synth deploy destroy invoke-ingestor

# ============================================================
# serverless-financial-etl — common dev commands
# Usage: make <target>
# ============================================================

PYTHON       := python3
PIP          := pip
PYTEST       := pytest
CDK          := cdk
AWS          := aws
REGION       := ap-south-1
STACK        := ServerlessFinancialEtl

# ============================================================
# Setup
# ============================================================

install:
	@echo "Installing test dependencies..."
	$(PIP) install pytest pytest-cov pandas numpy moto[s3] boto3 pyarrow requests psycopg2-binary
	@echo "Installing CDK dependencies..."
	$(PIP) install -r infrastructure/requirements.txt
	@echo "Done. Run 'make test' to verify."

# ============================================================
# Code quality
# ============================================================

lint:
	@echo "Running black..."
	black --check . --exclude=".venv|cdk.out|__pycache__"
	@echo "Running flake8..."
	flake8 . --max-line-length=100 --exclude=.venv,cdk.out,__pycache__
	@echo "Running isort..."
	isort --check . --skip=.venv --skip=cdk.out
	@echo "All lint checks passed."

format:
	@echo "Formatting code..."
	black . --exclude=".venv|cdk.out|__pycache__"
	isort . --skip=.venv --skip=cdk.out
	@echo "Done."

# ============================================================
# Testing
# ============================================================

test:
	@echo "Running test suite..."
	$(PYTEST) tests/ -v

test-cov:
	@echo "Running tests with coverage report..."
	$(PYTEST) tests/ -v \
		--cov=lambdas \
		--cov-report=term-missing \
		--cov-report=html:htmlcov \
		--cov-fail-under=50
	@echo "Coverage report saved to htmlcov/index.html"

test-transformer:
	$(PYTEST) tests/unit/test_transformer.py -v

test-ingestor:
	$(PYTEST) tests/unit/test_ingestor.py -v

test-loader:
	$(PYTEST) tests/unit/test_loader.py -v

# ============================================================
# CDK — Infrastructure
# ============================================================

synth:
	@echo "Synthesising CloudFormation template..."
	cd infrastructure && $(CDK) synth

deploy:
	@echo "Deploying stack to AWS ($(REGION))..."
	cd infrastructure && $(CDK) deploy --all --require-approval never

destroy:
	@echo "WARNING: This will destroy all AWS resources including RDS data."
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	cd infrastructure && $(CDK) destroy --all --force

diff:
	@echo "Showing diff between deployed and local stack..."
	cd infrastructure && $(CDK) diff

bootstrap:
	@echo "Bootstrapping CDK in $(REGION)..."
	cd infrastructure && $(CDK) bootstrap aws://$(AWS_ACCOUNT_ID)/$(REGION)

# ============================================================
# Local Lambda invocation (requires real AWS credentials + resources)
# ============================================================

invoke-ingestor:
	@echo "Manually triggering ingestor Lambda..."
	$(AWS) lambda invoke \
		--function-name etl-ingestor \
		--region $(REGION) \
		--payload '{}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/ingestor-response.json
	@cat /tmp/ingestor-response.json

invoke-transformer:
	@echo "Triggering transformer Lambda (requires raw_key)..."
	@test -n "$(RAW_KEY)" || (echo "Usage: make invoke-transformer RAW_KEY=raw/year=2025/month=05/day=08/data.json" && exit 1)
	$(AWS) lambda invoke \
		--function-name etl-transformer \
		--region $(REGION) \
		--payload '{"raw_key": "$(RAW_KEY)"}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/transformer-response.json
	@cat /tmp/transformer-response.json

invoke-loader:
	@echo "Triggering loader Lambda (requires processed_key)..."
	@test -n "$(PROCESSED_KEY)" || (echo "Usage: make invoke-loader PROCESSED_KEY=processed/year=2025/month=05/day=08/data.parquet" && exit 1)
	$(AWS) lambda invoke \
		--function-name etl-loader \
		--region $(REGION) \
		--payload '{"processed_key": "$(PROCESSED_KEY)"}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/loader-response.json
	@cat /tmp/loader-response.json

# ============================================================
# Cleanup
# ============================================================

clean:
	@echo "Cleaning build artifacts..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	rm -rf infrastructure/cdk.out
	@echo "Clean complete."

# ============================================================
# Help
# ============================================================

help:
	@echo ""
	@echo "serverless-financial-etl — available commands:"
	@echo ""
	@echo "  Setup:"
	@echo "    make install          Install all dependencies"
	@echo ""
	@echo "  Code quality:"
	@echo "    make lint             Run black + flake8 + isort checks"
	@echo "    make format           Auto-format code with black + isort"
	@echo ""
	@echo "  Testing:"
	@echo "    make test             Run full test suite (89 tests)"
	@echo "    make test-cov         Run tests with coverage report"
	@echo "    make test-transformer Run transformer tests only"
	@echo "    make test-ingestor    Run ingestor tests only"
	@echo "    make test-loader      Run loader tests only"
	@echo ""
	@echo "  Infrastructure:"
	@echo "    make synth            CDK synth (dry run)"
	@echo "    make deploy           Deploy to AWS"
	@echo "    make destroy          Tear down all AWS resources"
	@echo "    make diff             Show infrastructure diff"
	@echo ""
	@echo "  Lambda invocation:"
	@echo "    make invoke-ingestor  Trigger ingestor manually"
	@echo "    make invoke-transformer RAW_KEY=raw/..."
	@echo "    make invoke-loader PROCESSED_KEY=processed/..."
	@echo ""
	@echo "  Cleanup:"
	@echo "    make clean            Remove __pycache__, htmlcov, cdk.out"
	@echo ""