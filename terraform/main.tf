# ABOUTME: All AWS infrastructure for jamf-patch-sync.
# ABOUTME: Lambda, EventBridge Scheduler, IAM, SSM, SNS, CloudWatch alarms.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}

# --- Lambda deployment package ---

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/.build/${var.project_name}.zip"

  source {
    content  = file("${path.module}/../lambda/handler.py")
    filename = "handler.py"
  }

  source {
    content  = file("${path.module}/../lambda/apps.json")
    filename = "apps.json"
  }
}

# --- SSM Parameter Store (credentials) ---

resource "aws_ssm_parameter" "te_username" {
  name        = "/${var.project_name}/te-username"
  description = "Jamf Title Editor account username, read by the ${var.project_name} Lambda at runtime"
  type        = "SecureString"
  value       = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "te_password" {
  name        = "/${var.project_name}/te-password"
  description = "Jamf Title Editor account password, read by the ${var.project_name} Lambda at runtime"
  type        = "SecureString"
  value       = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# --- IAM: Lambda execution role ---


resource "aws_iam_role" "lambda" {
  name        = "${var.project_name}-lambda"
  description = "Execution role for the ${var.project_name} Lambda: read Title Editor credentials from SSM, write CloudWatch logs, publish JamfPatchSync metrics"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

data "aws_secretsmanager_secret" "jamf_pro_readonly" {
  count = var.jamf_pro_secret_name != "" ? 1 : 0
  name  = var.jamf_pro_secret_name
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "SSMReadCredentials"
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          aws_ssm_parameter.te_username.arn,
          aws_ssm_parameter.te_password.arn,
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "JamfPatchSync"
          }
        }
      },
      {
        Sid      = "SNSFailureAlerts"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      },
      ],
      var.jamf_pro_secret_name != "" ? [{
        Sid      = "JamfProReadonlySecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = data.aws_secretsmanager_secret.jamf_pro_readonly[0].arn
      }] : []
    )
  })
}

# --- Lambda function ---

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "patch_sync" {
  function_name    = var.project_name
  description      = "Pushes latest vendor app versions into Jamf Title Editor patch definitions, config-driven by bundled apps.json. Runs twice daily via EventBridge Scheduler, reads Title Editor credentials from SSM. Code: github.com/07-C9/jamf-title-editor-sync"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  timeout          = 90
  memory_size      = 128
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  reserved_concurrent_executions = 1

  environment {
    variables = {
      TITLE_EDITOR_URL                              = var.title_editor_url
      TITLE_EDITOR_TITLE_ID                         = var.title_editor_title_id
      TITLE_EDITOR_GMETRIX_TITLE_ID                 = var.title_editor_gmetrix_title_id
      TITLE_EDITOR_MACADMINS_PYTHON_TITLE_ID        = var.title_editor_macadmins_python_title_id
      TITLE_EDITOR_SCREENCONNECT_TITLE_ID           = var.title_editor_screenconnect_title_id
      TITLE_EDITOR_PROMETHEAN_SCREEN_SHARE_TITLE_ID = var.title_editor_promethean_screen_share_title_id
      TITLE_EDITOR_WASECUREBROWSER_TITLE_ID         = var.title_editor_wasecurebrowser_title_id
      TITLE_EDITOR_OUTSET_TITLE_ID                  = var.title_editor_outset_title_id
      TITLE_EDITOR_UTILUTI_TITLE_ID                 = var.title_editor_utiluti_title_id
      TITLE_EDITOR_DYMO_CONNECT_TITLE_ID            = var.title_editor_dymo_connect_title_id
      TITLE_EDITOR_DRC_INSIGHT_TITLE_ID             = var.title_editor_drc_insight_title_id
      SSM_USERNAME_PATH                             = aws_ssm_parameter.te_username.name
      SSM_PASSWORD_PATH                             = aws_ssm_parameter.te_password.name
      ALERT_TOPIC_ARN                               = aws_sns_topic.alerts.arn
      JAMF_PRO_URL                                  = var.jamf_pro_url
      JAMF_PRO_SECRET_ID                            = var.jamf_pro_secret_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# Scheduled runs are idempotent and re-run within 12 hours; retrying a failed
# async invocation only repeats the same failure and multiplies error noise.
resource "aws_lambda_function_event_invoke_config" "patch_sync" {
  function_name          = aws_lambda_function.patch_sync.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "scheduler" {
  statement_id  = "AllowEventBridgeScheduler"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.patch_sync.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.twice_daily.arn
}

# --- EventBridge Scheduler ---

resource "aws_iam_role" "scheduler" {
  name        = "${var.project_name}-scheduler"
  description = "Lets EventBridge Scheduler invoke the ${var.project_name} Lambda on its twice-daily schedule"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.project_name}-scheduler-policy"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.patch_sync.arn
    }]
  })
}

resource "aws_scheduler_schedule" "twice_daily" {
  name        = "${var.project_name}-schedule"
  description = "Invokes the ${var.project_name} Lambda twice daily to keep Title Editor patch definitions current with vendor releases"
  group_name  = "default"

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  target {
    arn      = aws_lambda_function.patch_sync.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 21600
    }
  }
}

# --- SNS (failure alerts) ---

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --- CloudWatch Alarms ---

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-errors"
  alarm_description   = "A twice-daily Title Editor sync run raised an error. The Lambda emails failure detail (which app, what error) to this same SNS topic; see that email or the Lambda logs. Missing data is ignored because the function only runs twice a day, so ALARM holds until a later run completes clean - the OK email means an actual clean run."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "ignore"

  dimensions = {
    FunctionName = aws_lambda_function.patch_sync.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  alarm_name          = "${var.project_name}-duration"
  alarm_description   = "A Title Editor sync run took over 75s of the 90s timeout - a vendor or Jamf Pro endpoint is probably hanging (healthy runs take ~25-40s; the Jamf Pro patch config list alone is budgeted 45s because it answers in 13-17s). Check the Lambda logs for which call was slow. Missing data is ignored (function runs twice a day); ALARM holds until a later run finishes fast."
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 75000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "ignore"

  dimensions = {
    FunctionName = aws_lambda_function.patch_sync.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "no_invocations" {
  alarm_name          = "${var.project_name}-no-invocations"
  alarm_description   = "No Lambda invocations in 24 hours - scheduler may be broken"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    FunctionName = aws_lambda_function.patch_sync.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "jamfpro_definition_lag" {
  count               = var.jamf_pro_url != "" ? 1 : 0
  alarm_name          = "${var.project_name}-jamfpro-definition-lag"
  alarm_description   = "Jamf Pro's ingested patch definitions have diverged from Title Editor for two consecutive sync runs (roughly 24 hours). Brief lag right after a version push is normal (Jamf Pro polls Title Editor on its own schedule) and will not fire this. Persistent divergence means Jamf Pro stopped pulling from Title Editor: re-save the external patch source under Settings > Computer management > Patch management to force the M2M reconnect, then confirm the next runs post JamfProDefinitionLag=0. Metric comes from the sync Lambda's drift check; the run's 'Jamf Pro definition lag' log lines name the titles."
  namespace           = "JamfPatchSync"
  metric_name         = "JamfProDefinitionLag"
  statistic           = "Minimum"
  period              = 43200
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "ignore"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "minimum_version_changed" {
  alarm_name          = "${var.project_name}-minimum-version-changed"
  alarm_description   = "A vendor changed the lowest version it will still accept, and the live value no longer matches the one recorded in lambda/apps.json. Nothing on any Mac changed: what changed is whether machines behind the newest release are merely out of date or now refused outright. For DRC INSIGHT the number comes from WIDA's public sb-versions feed, and a raised floor means students on an older build cannot start an ACCESS test. Patch reporting cannot show this, because it only ever compares against the newest release. Confirm the new number, make sure the required version is deployed ahead of the testing window, then update the 'known' value in lambda/apps.json and redeploy to acknowledge it. The sync Lambda's 'Vendor minimum version changed' log lines name the app, the live value and the recorded value. Missing data holds the current state rather than clearing it, because the metric is only published when the check actually answered."
  namespace           = "JamfPatchSync"
  metric_name         = "MinimumVersionChanged"
  statistic           = "Maximum"
  period              = 43200
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "ignore"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "download_url_failures" {
  alarm_name          = "${var.project_name}-download-url-failures"
  alarm_description   = "An Installomator download URL went dark (e.g. Adobe CC build-path change). The Lambda's _run_download_checks emits DownloadUrlCheckFailures; inspect the Lambda logs for which arch/URL failed."
  namespace           = "JamfPatchSync"
  metric_name         = "DownloadUrlCheckFailures"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "ignore"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
