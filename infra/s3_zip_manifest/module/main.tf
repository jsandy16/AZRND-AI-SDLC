# Rendered by pkf Studio 0.10.10 — do not edit; change the specs and render again.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "name" { type = string }
variable "create_queue" { type = bool }
variable "visibility_timeout_seconds" { type = number }
variable "max_receive_count" { type = number }
variable "sender_bucket_arns" { type = list(string) }
variable "account_id" { type = string }
variable "worker_policy_json" { type = string }
variable "create_worker_policy" {
  type    = bool
  default = false
}
variable "alarm_actions" {
  type    = list(string)
  default = []
}

resource "aws_sqs_queue" "dlq" {
  count                     = var.create_queue ? 1 : 0
  name                      = "${var.name}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "queue" {
  count                      = var.create_queue ? 1 : 0
  name                       = var.name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[0].arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sqs_queue_policy" "queue" {
  count     = var.create_queue && length(var.sender_bucket_arns) > 0 ? 1 : 0
  queue_url = aws_sqs_queue.queue[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "S3Notifications"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.queue[0].arn
      Condition = {
        ArnLike      = { "aws:SourceArn" = var.sender_bucket_arns }
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  count               = var.create_queue ? 1 : 0
  alarm_name          = "${var.name}-dlq-not-empty"
  alarm_description   = "Messages the pipeline could not process (see the DAG's failed runs)"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
}

resource "aws_iam_policy" "worker" {
  count       = var.create_worker_policy ? 1 : 0
  name        = "${var.name}-worker"
  description = "What the Airflow worker needs for this pipeline (pkf Studio)"
  policy      = var.worker_policy_json
}

output "queue_url" { value = var.create_queue ? aws_sqs_queue.queue[0].url : null }
output "queue_arn" { value = var.create_queue ? aws_sqs_queue.queue[0].arn : null }
output "dlq_arn" { value = var.create_queue ? aws_sqs_queue.dlq[0].arn : null }
output "worker_policy_arn" { value = var.create_worker_policy ? aws_iam_policy.worker[0].arn : null }
