variable "region" {
  type        = string
  description = "AWS region for the deployment."
}

variable "environment" {
  type        = string
  description = "Environment name (dev, staging, production)."
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging, or production."
  }
}

variable "vpc_id" {
  type        = string
  description = "VPC that hosts the RDS instance."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "At least two private subnet IDs in different AZs."
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide at least two private subnets for Multi-AZ RDS."
  }
}

variable "app_security_group_ids" {
  type        = list(string)
  description = "Security groups of the Kubernetes workers/API that need database access."
}

variable "db_name" {
  type        = string
  default     = "marketscope"
  description = "Initial PostgreSQL database name."
}

variable "db_username" {
  type        = string
  default     = "marketscope"
  description = "RDS master username."
}

variable "db_instance_class" {
  type        = string
  default     = "db.r6g.large"
  description = "RDS instance class."
}

variable "db_allocated_storage_gb" {
  type        = number
  default     = 100
  description = "Initial allocated storage in GiB."
}

variable "db_max_allocated_storage_gb" {
  type        = number
  default     = 1000
  description = "Storage autoscaling ceiling in GiB."
}

variable "backup_retention_days" {
  type        = number
  default     = 14
  description = "Automated backup retention; must meet the deployment's documented RPO."
  validation {
    condition     = var.backup_retention_days >= 7
    error_message = "backup_retention_days must be at least 7."
  }
}

variable "deletion_protection" {
  type        = bool
  default     = true
  description = "Keep RDS and bucket deletion protection enabled in production."
}

variable "multi_az" {
  type        = bool
  default     = true
  description = "Enable a Multi-AZ RDS standby."
}

variable "rpo_hours" {
  type        = number
  default     = 1
  description = "Declared recovery point objective for runbooks and tags."
}

variable "rto_hours" {
  type        = number
  default     = 4
  description = "Declared recovery time objective for runbooks and tags."
}
