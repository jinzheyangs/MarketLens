data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

resource "aws_kms_key" "data" {
  description             = "MarketLens ${var.environment} data encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "data" {
  name          = "alias/marketlens-${var.environment}"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_s3_bucket" "documents" {
  bucket        = "${var.environment}-marketlens-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "deny_insecure_transport" {
  bucket = aws_s3_bucket.documents.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.documents.arn,
        "${aws_s3_bucket.documents.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  # Object expiry is a tenant data-lifecycle decision. Add explicit rules only
  # after the retention policy is approved; do not silently expire evidence.
}

resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "database" {
  name                    = "marketlens/${var.environment}/database"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db.result
    host     = aws_db_instance.primary.address
    port     = aws_db_instance.primary.port
    database = var.db_name
    url      = "postgresql+asyncpg://${var.db_username}:${urlencode(random_password.db.result)}@${aws_db_instance.primary.address}:${aws_db_instance.primary.port}/${var.db_name}"
  })
}

resource "aws_security_group" "database" {
  name_prefix = "marketlens-${var.environment}-db-"
  description = "Allow application security groups to reach PostgreSQL"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = var.app_security_group_ids
    content {
      description     = "Application access"
      from_port       = 5432
      to_port         = 5432
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }

  # No egress rules: the database only receives connections.

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_subnet_group" "database" {
  name       = "marketlens-${var.environment}"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "database" {
  name   = "marketlens-${var.environment}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "primary" {
  identifier                      = "marketlens-${var.environment}"
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_allocated_storage_gb
  max_allocated_storage           = var.db_max_allocated_storage_gb
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.data.arn
  db_name                         = var.db_name
  username                        = var.db_username
  password                        = random_password.db.result
  db_subnet_group_name            = aws_db_subnet_group.database.name
  parameter_group_name            = aws_db_parameter_group.database.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  multi_az                        = var.multi_az
  backup_retention_period         = var.backup_retention_days
  backup_window                   = "02:00-03:00"
  maintenance_window              = "Sun:03:30-Sun:04:30"
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "marketlens-${var.environment}-final"
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.data.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  apply_immediately               = false
  tags = {
    RPO = "${var.rpo_hours}h"
    RTO = "${var.rto_hours}h"
  }
}
