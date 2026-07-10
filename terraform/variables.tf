# ABOUTME: Input variables for the jamf-patch-sync Terraform deployment.
# ABOUTME: Configures Title Editor connection, alerting, and schedule.

variable "title_editor_url" {
  description = "Title Editor base URL (e.g., https://yourorg.appcatalog.jamfcloud.com)"
  type        = string
}

variable "title_editor_title_id" {
  description = "Numeric ID of the Chrome software title in Title Editor"
  type        = string
}

variable "title_editor_gmetrix_title_id" {
  description = "Numeric ID of the GMetrix SMSe software title in Title Editor"
  type        = string
}

variable "title_editor_macadmins_python_title_id" {
  description = "Numeric ID of the MacAdmins Python software title in Title Editor"
  type        = string
}

variable "title_editor_screenconnect_title_id" {
  description = "Numeric ID of the ScreenConnect Client software title in Title Editor"
  type        = string
}

variable "title_editor_promethean_screen_share_title_id" {
  description = "Numeric ID of the Promethean Screen Share software title in Title Editor"
  type        = string
}

variable "title_editor_wasecurebrowser_title_id" {
  description = "Numeric ID of the Washington Secure Browser software title in Title Editor"
  type        = string
}

variable "title_editor_outset_title_id" {
  description = "Numeric ID of the Outset software title in Title Editor"
  type        = string
}

variable "title_editor_utiluti_title_id" {
  description = "Numeric ID of the utiluti software title in Title Editor"
  type        = string
}

variable "alert_email" {
  description = "Email address for failure notifications"
  type        = string
}

variable "jamf_pro_url" {
  description = "Jamf Pro base URL for the definition drift check (e.g., https://yourorg.jamfcloud.com). Leave empty to disable the check."
  type        = string
  default     = ""
}

variable "jamf_pro_secret_name" {
  description = "Secrets Manager secret holding a read-only Jamf Pro API client as JSON keys client_id and client_secret (needs Read Patch Management Software Titles). Leave empty to disable the drift check."
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-west-2"
}

variable "schedule_timezone" {
  description = "IANA timezone for the EventBridge schedule"
  type        = string
  default     = "America/Los_Angeles"
}

variable "project_name" {
  description = "Name prefix for all AWS resources"
  type        = string
  default     = "jamf-title-editor-sync"
}

variable "schedule_expression" {
  description = "EventBridge cron expression (default: 6 AM and 6 PM)"
  type        = string
  default     = "cron(0 6,18 * * ? *)"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}
