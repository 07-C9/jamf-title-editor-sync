# ABOUTME: Terraform outputs for post-deploy reference.
# ABOUTME: Shows ARNs and paths needed for testing and credential setup.

output "lambda_function_arn" {
  description = "Lambda function ARN for manual test invocations"
  value       = aws_lambda_function.patch_sync.arn
}

output "lambda_function_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.patch_sync.function_name
}

output "sns_topic_arn" {
  description = "SNS topic ARN - confirm email subscription after first deploy"
  value       = aws_sns_topic.alerts.arn
}

output "log_group_name" {
  description = "CloudWatch log group for tailing: aws logs tail <name> --follow"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "ssm_username_path" {
  description = "SSM parameter path for Title Editor username"
  value       = aws_ssm_parameter.te_username.name
}

output "ssm_password_path" {
  description = "SSM parameter path for Title Editor password"
  value       = aws_ssm_parameter.te_password.name
}

output "manual_test_command" {
  description = "Run this to test the Lambda manually"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.patch_sync.function_name} /tmp/jamf-patch-sync-response.json && cat /tmp/jamf-patch-sync-response.json"
}
