output "database_secret_arn" {
  description = "Secrets Manager ARN containing the PostgreSQL DSN. Sync it into the Kubernetes runtime Secret."
  value       = aws_secretsmanager_secret.database.arn
}

output "database_endpoint" {
  description = "RDS PostgreSQL endpoint (host:port)."
  value       = aws_db_instance.primary.endpoint
}

output "documents_bucket" {
  description = "S3 bucket for documents, snapshots, and exports."
  value       = aws_s3_bucket.documents.bucket
}

output "kms_key_arn" {
  description = "KMS key used for RDS, S3, and Secrets Manager."
  value       = aws_kms_key.data.arn
}
