"""
infrastructure/etl_stack.py

Full AWS CDK stack for the serverless-financial-etl pipeline.

Provisions:
  - S3 buckets (raw + processed), lifecycle rules
  - Secrets Manager secret for DB credentials + Alpha Vantage API key
  - RDS PostgreSQL (db.t3.micro, Free Tier eligible)
  - VPC with private/public subnets for RDS isolation
  - Lambda functions (ingestor, transformer, loader) with layers
  - EventBridge rule (daily cron at 06:00 UTC)
  - Step Functions-style chaining via Lambda destinations
  - SNS topic + email subscription for failure alerts
  - CloudWatch log groups with 14-day retention
  - IAM roles with least-privilege policies

Deploy:
  cdk deploy --all

Tear down:
  cdk destroy --all
"""

import os
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
)
from constructs import Construct


class EtlStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alert_email = self.node.try_get_context("alert_email") or "aliasgarbadri5352@gmail.com"
        target_symbols = self.node.try_get_context("target_symbols") or "AAPL,MSFT,GOOGL,AMZN,TSLA"
        db_name = "financial_etl"

        # ----------------------------------------------------------------
        # 1. VPC — isolated private subnet for RDS
        # ----------------------------------------------------------------
        vpc = ec2.Vpc(
            self, "EtlVpc",
            max_azs=2,
            nat_gateways=0,  # No NAT — Lambdas use VPC endpoints or public subnet
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # Security group for Lambda functions
        lambda_sg = ec2.SecurityGroup(
            self, "LambdaSG",
            vpc=vpc,
            description="Security group for ETL Lambda functions",
            allow_all_outbound=True,
        )

        # Security group for RDS — only accepts traffic from Lambda SG
        rds_sg = ec2.SecurityGroup(
            self, "RdsSG",
            vpc=vpc,
            description="Security group for RDS PostgreSQL",
            allow_all_outbound=False,
        )
        rds_sg.add_ingress_rule(
            peer=lambda_sg,
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL from Lambda functions",
        )

        # ----------------------------------------------------------------
        # 2. S3 buckets
        # ----------------------------------------------------------------
        raw_bucket = s3.Bucket(
            self, "RawBucket",
            bucket_name=f"etl-raw-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireRawAfter90Days",
                    enabled=True,
                    expiration=Duration.days(90),
                )
            ],
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        processed_bucket = s3.Bucket(
            self, "ProcessedBucket",
            bucket_name=f"etl-processed-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireProcessedAfter180Days",
                    enabled=True,
                    expiration=Duration.days(180),
                )
            ],
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        # ----------------------------------------------------------------
        # 3. Secrets Manager
        # ----------------------------------------------------------------
        # Alpha Vantage API key — populate manually after deploy:
        #   aws secretsmanager put-secret-value \
        #     --secret-id etl/alpha-vantage \
        #     --secret-string '{"api_key": "YOUR_KEY"}'
        av_secret = secretsmanager.Secret(
            self, "AlphaVantageSecret",
            secret_name="etl/alpha-vantage",
            description="Alpha Vantage API key for market data ingestion",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"api_key": "REPLACE_ME"}',
                generate_string_key="placeholder",
            ),
        )

        # RDS credentials — CDK auto-generates a secure password
        db_secret = secretsmanager.Secret(
            self, "DbSecret",
            secret_name="etl/rds-credentials",
            description="RDS PostgreSQL credentials for ETL loader Lambda",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username": "etl_user", "dbname": "{db_name}"}}',
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
        )

        # ----------------------------------------------------------------
        # 4. RDS PostgreSQL
        # ----------------------------------------------------------------
        db_subnet_group = rds.SubnetGroup(
            self, "DbSubnetGroup",
            description="Subnet group for ETL RDS instance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
        )

        db_instance = rds.DatabaseInstance(
            self, "EtlDatabase",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_15
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.MICRO,  # db.t3.micro — Free Tier eligible
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            subnet_group=db_subnet_group,
            security_groups=[rds_sg],
            credentials=rds.Credentials.from_secret(db_secret),
            database_name=db_name,
            allocated_storage=20,
            max_allocated_storage=100,
            backup_retention=Duration.days(7),
            deletion_protection=False,  # Set True for production
            removal_policy=RemovalPolicy.DESTROY,
            publicly_accessible=False,
            storage_encrypted=True,
            multi_az=False,  # Single-AZ for cost — set True for production
        )

        # ----------------------------------------------------------------
        # 5. SNS alert topic
        # ----------------------------------------------------------------
        alert_topic = sns.Topic(
            self, "AlertTopic",
            topic_name="etl-pipeline-alerts",
            display_name="ETL Pipeline Failure Alerts",
        )
        alert_topic.add_subscription(
            subscriptions.EmailSubscription(alert_email)
        )

        # ----------------------------------------------------------------
        # 6. Shared Lambda IAM role
        # ----------------------------------------------------------------
        lambda_role = iam.Role(
            self, "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Shared IAM role for ETL Lambda functions",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
            ],
        )

        # S3 permissions
        raw_bucket.grant_read_write(lambda_role)
        processed_bucket.grant_read_write(lambda_role)

        # Secrets Manager permissions
        av_secret.grant_read(lambda_role)
        db_secret.grant_read(lambda_role)

        # SNS publish for alerts
        alert_topic.grant_publish(lambda_role)

        # ----------------------------------------------------------------
        # 7. Shared Lambda configuration
        # ----------------------------------------------------------------
        common_env = {
            "RAW_BUCKET": raw_bucket.bucket_name,
            "PROCESSED_BUCKET": processed_bucket.bucket_name,
            "TARGET_SYMBOLS": target_symbols,
            "ALPHA_VANTAGE_SECRET": av_secret.secret_name,
            "DB_SECRET_NAME": db_secret.secret_name,
            "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            "AWS_REGION_NAME": self.region,
        }

        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_11,
            role=lambda_role,
            timeout=Duration.minutes(5),
            memory_size=512,
            environment=common_env,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[lambda_sg],
            tracing=lambda_.Tracing.ACTIVE,
        )

        # ----------------------------------------------------------------
        # 8. Lambda functions
        # ----------------------------------------------------------------

        # -- Ingestor --
        ingestor_log_group = logs.LogGroup(
            self, "IngestorLogGroup",
            log_group_name="/aws/lambda/etl-ingestor",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        ingestor_fn = lambda_.Function(
            self, "IngestorFunction",
            function_name="etl-ingestor",
            code=lambda_.Code.from_asset(
                "../lambdas/ingestor",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_11.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output"
                    ],
                },
            ),
            handler="handler.handler",
            description="Fetches OHLCV data from Alpha Vantage and writes raw JSON to S3",
            **common_lambda_kwargs,
        )

        # -- Transformer --
        transformer_log_group = logs.LogGroup(
            self, "TransformerLogGroup",
            log_group_name="/aws/lambda/etl-transformer",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        transformer_fn = lambda_.Function(
            self, "TransformerFunction",
            function_name="etl-transformer",
            code=lambda_.Code.from_asset(
                "../lambdas/transformer",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_11.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output"
                    ],
                },
            ),
            handler="handler.handler",
            description="Reads raw JSON from S3, transforms with pandas, writes Parquet",
            memory_size=1024,  # pandas needs more memory
            **{k: v for k, v in common_lambda_kwargs.items() if k != "memory_size"},
        )

        # -- Loader --
        loader_log_group = logs.LogGroup(
            self, "LoaderLogGroup",
            log_group_name="/aws/lambda/etl-loader",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        loader_fn = lambda_.Function(
            self, "LoaderFunction",
            function_name="etl-loader",
            code=lambda_.Code.from_asset(
                "../lambdas/loader",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_11.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output"
                    ],
                },
            ),
            handler="handler.handler",
            description="Reads processed Parquet from S3 and upserts into RDS PostgreSQL",
            **common_lambda_kwargs,
        )

        # Allow loader to reach RDS
        db_instance.connections.allow_from(
            loader_fn,
            ec2.Port.tcp(5432),
            "Allow loader Lambda to connect to RDS",
        )

        # ----------------------------------------------------------------
        # 9. EventBridge rule — trigger ingestor daily at 06:00 UTC
        # ----------------------------------------------------------------
        rule = events.Rule(
            self, "DailyTrigger",
            rule_name="etl-daily-trigger",
            description="Triggers the ETL ingestor Lambda every weekday at 06:00 UTC",
            schedule=events.Schedule.cron(
                minute="0",
                hour="6",
                week_day="MON-FRI",  # Weekdays only — markets are closed weekends
                month="*",
                year="*",
            ),
        )
        rule.add_target(targets.LambdaFunction(ingestor_fn))

        # ----------------------------------------------------------------
        # 10. CloudWatch alarms — alert on Lambda errors
        # ----------------------------------------------------------------
        for fn, name in [
            (ingestor_fn, "Ingestor"),
            (transformer_fn, "Transformer"),
            (loader_fn, "Loader"),
        ]:
            alarm = cloudwatch.Alarm(
                self, f"{name}ErrorAlarm",
                alarm_name=f"etl-{name.lower()}-errors",
                alarm_description=f"ETL {name} Lambda error rate > 0",
                metric=fn.metric_errors(
                    period=Duration.minutes(5),
                    statistic="Sum",
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # ----------------------------------------------------------------
        # 11. CloudFormation outputs
        # ----------------------------------------------------------------
        CfnOutput(self, "RawBucketName",
            value=raw_bucket.bucket_name,
            description="S3 bucket for raw JSON ingestion output",
        )
        CfnOutput(self, "ProcessedBucketName",
            value=processed_bucket.bucket_name,
            description="S3 bucket for transformed Parquet files",
        )
        CfnOutput(self, "RdsEndpoint",
            value=db_instance.db_instance_endpoint_address,
            description="RDS PostgreSQL endpoint — use for direct queries",
        )
        CfnOutput(self, "DbSecretArn",
            value=db_secret.secret_arn,
            description="Secrets Manager ARN for DB credentials",
        )
        CfnOutput(self, "IngestorFunctionArn",
            value=ingestor_fn.function_arn,
            description="Ingestor Lambda ARN — invoke manually to trigger pipeline",
        )
        CfnOutput(self, "AlertTopicArn",
            value=alert_topic.topic_arn,
            description="SNS topic ARN for pipeline failure alerts",
        )