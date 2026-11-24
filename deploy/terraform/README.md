# Terraform sample (AWS)

This root module provisions the stateful dependencies MarketLens expects in
production:

- Multi-AZ RDS PostgreSQL 16 with KMS encryption, automated backups, deletion
  protection, Performance Insights, and PostgreSQL/upgrade logs.
- An S3 bucket with versioning, KMS default encryption, public-access blocks,
  TLS-only policy, and incomplete-multipart cleanup.
- Secrets Manager entry containing the database DSN.
- A KMS key with rotation enabled.

It does **not** create the VPC, EKS cluster, Kafka/MSK, Temporal, Qdrant, DNS,
certificates, or IAM roles for workloads. Those are environment-specific and
must be supplied by the platform team.

## Usage

```bash
cd deploy/terraform
terraform init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=marketlens/production/terraform.tfstate" \
  -backend-config="region=<region>" \
  -backend-config="dynamodb_table=<lock-table>"
terraform plan -var-file=production.tfvars
terraform apply -var-file=production.tfvars
```

Keep one state and one tfvars file per environment. Never share a database or
bucket between production and non-production. Encrypt remote state and restrict
access to it; the state contains the generated database password.

## After apply

1. Sync `database_secret_arn` into your ExternalSecret/secret manager pipeline.
2. Configure the application `DATABASE_URL` with TLS parameters required by RDS.
3. Set the application KMS key/S3 bucket values through the runtime Secret.
4. Run `alembic upgrade head` through the Helm migration Job.
5. Record the actual backup retention and measured restore time in the
   environment runbook.

## Verification status

`terraform validate/plan/apply` was **not** run in this repository environment
because Terraform and cloud credentials are absent. Treat this as a reviewed
starting point, not a validated deployment.
