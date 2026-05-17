#!/usr/bin/env python3
"""
infrastructure/app.py

CDK app entry point.
Deploy with: cdk deploy --all

Prerequisites:
  pip install -r infrastructure/requirements.txt
  cdk bootstrap aws://YOUR_ACCOUNT_ID/ap-south-1
"""

import os
import aws_cdk as cdk
from etl_stack import EtlStack

app = cdk.App()

EtlStack(
    app,
    "ServerlessFinancialEtl",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="Serverless financial ETL pipeline — Lambda, S3, RDS, EventBridge",
)

app.synth()