# Rendered by pkf Studio 0.10.10 — environment `ppt` of pipeline `s3_zip_manifest`.
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
variable "name" { type = string }
variable "create_queue" { type = bool }
variable "visibility_timeout_seconds" { type = number }
variable "max_receive_count" { type = number }
variable "sender_bucket_arns" { type = list(string) }
variable "worker_policy_json" { type = string }
variable "create_worker_policy" { type = bool }

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = var.tags }
}

module "pipeline" {
  source                     = "../../module"
  name                       = var.name
  create_queue               = var.create_queue
  visibility_timeout_seconds = var.visibility_timeout_seconds
  max_receive_count          = var.max_receive_count
  sender_bucket_arns         = var.sender_bucket_arns
  account_id                 = var.account_id
  worker_policy_json         = var.worker_policy_json
  create_worker_policy       = var.create_worker_policy
}

output "queue_url" { value = module.pipeline.queue_url }
output "queue_arn" { value = module.pipeline.queue_arn }
output "worker_policy_arn" { value = module.pipeline.worker_policy_arn }
