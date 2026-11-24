terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Configure a remote backend per environment (S3 + DynamoDB lock) before
  # planning against shared accounts. Local state is unsafe for production.
  # backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = "marketlens"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
