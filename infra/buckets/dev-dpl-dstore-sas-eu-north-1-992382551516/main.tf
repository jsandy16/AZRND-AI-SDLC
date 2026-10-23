# Rendered by pkf Studio 0.10.10 — S3 event notifications of bucket `dev-dpl-dstore-sas-eu-north-1-992382551516` for every pipeline in this repository.
# Terraform owns the bucket's WHOLE notification configuration: a bucket with entries this repository does not own is
# never planned (the Studio writes handoff.json for the bucket's owner instead).
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

variable "region" { type = string }
variable "account_id" { type = string }
variable "tags" { type = map(string) }

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = var.tags }
}

resource "aws_s3_bucket_notification" "this" {
  bucket = "dev-dpl-dstore-sas-eu-north-1-992382551516"
  queue {
    id            = "pkf-s3_zip_manifest"
    queue_arn     = "arn:aws:sqs:eu-north-1:992382551516:pkf-dev-s3-zip-manifest"
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "incoming/"
    filter_suffix = ".zip"
  }
}
