#############################################
# Minimal Telemetry Ingestion -> Features
#############################################
# Components provided:
# 1. API Gateway (REST) with POST /validate
# 2. Validation Lambda (forwards valid events to Kinesis)
# 3. Kinesis Stream (raw telemetry events)
# 4. Feature Extraction Lambda consuming Kinesis stream
#
# NOTE: This intentionally omits DynamoDB, S3, pricing engine, schedules, etc.
# to keep scope aligned with user request.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}
variable "env" {
  type    = string
  default = "dev"
}
variable "validation_source_dir" {
  type    = string
  default = "../../src/aws_lambda/validation"
}
variable "feature_source_dir" {
  type    = string
  default = "../../src/aws_lambda/feature_extraction"
}
variable "kinesis_shard_count" {
  type    = number
  default = 1
}

locals {
  name_prefix = "telemetry-min-${var.env}"
  tags        = { Environment = var.env, Service = "telemetry-min" }
}

resource "aws_kinesis_stream" "events" {
  name             = "${local.name_prefix}-events"
  shard_count      = var.kinesis_shard_count
  retention_period = 24
  stream_mode_details {
    stream_mode = "PROVISIONED"
  }
  tags = local.tags
}

data "archive_file" "validation_zip" {
  type        = "zip"
  source_dir  = var.validation_source_dir
  output_path = "validation_lambda.zip"
}

data "archive_file" "feature_zip" {
  type        = "zip"
  source_dir  = var.feature_source_dir
  output_path = "feature_lambda.zip"
}

resource "aws_iam_role" "validation_role" {
  name = "${local.name_prefix}-validation-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "validation_basic" {
  role       = aws_iam_role.validation_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "validation_kinesis_write" {
  name = "${local.name_prefix}-validation-kinesis"
  role = aws_iam_role.validation_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Action = ["kinesis:PutRecord", "kinesis:PutRecords"], Resource = aws_kinesis_stream.events.arn }]
  })
}

resource "aws_iam_role" "feature_role" {
  name = "${local.name_prefix}-feature-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "feature_basic" {
  role       = aws_iam_role.feature_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "feature_kinesis_read" {
  name = "${local.name_prefix}-feature-kinesis"
  role = aws_iam_role.feature_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Action = ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream", "kinesis:ListShards"], Resource = aws_kinesis_stream.events.arn }]
  })
}

resource "aws_lambda_function" "validation" {
  function_name = "${local.name_prefix}-validation"
  role          = aws_iam_role.validation_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  filename         = data.archive_file.validation_zip.output_path
  source_code_hash = data.archive_file.validation_zip.output_base64sha256
  timeout       = 10
  memory_size   = 256
  environment {
    variables = {
      KINESIS_STREAM_NAME = aws_kinesis_stream.events.name
      ENV                 = var.env
    }
  }
  depends_on = [aws_iam_role_policy_attachment.validation_basic, aws_iam_role_policy.validation_kinesis_write]
  tags       = local.tags
}

resource "aws_lambda_function" "feature_extraction" {
  function_name = "${local.name_prefix}-feature-extraction"
  role          = aws_iam_role.feature_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  filename         = data.archive_file.feature_zip.output_path
  source_code_hash = data.archive_file.feature_zip.output_base64sha256
  timeout       = 30
  memory_size   = 512
  environment {
    variables = {
      FEATURES_STREAM_NAME = aws_kinesis_stream.events.name
      ENV                  = var.env
    }
  }
  depends_on = [aws_iam_role_policy_attachment.feature_basic, aws_iam_role_policy.feature_kinesis_read]
  tags       = local.tags
}

resource "aws_lambda_event_source_mapping" "feature_kinesis" {
  event_source_arn  = aws_kinesis_stream.events.arn
  function_name     = aws_lambda_function.feature_extraction.arn
  starting_position = "LATEST"
  batch_size        = 100
  maximum_batching_window_in_seconds = 5
  enabled           = true
}

resource "aws_api_gateway_rest_api" "validation" {
  name        = "${local.name_prefix}-api"
  description = "Validation ingest API"
}

resource "aws_api_gateway_resource" "validate" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  parent_id   = aws_api_gateway_rest_api.validation.root_resource_id
  path_part   = "validate"
}

resource "aws_api_gateway_method" "post_validate" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.validate.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "post_validate_integration" {
  rest_api_id             = aws_api_gateway_rest_api.validation.id
  resource_id             = aws_api_gateway_resource.validate.id
  http_method             = aws_api_gateway_method.post_validate.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.validation.invoke_arn
}

resource "aws_lambda_permission" "apigw_invoke_validation" {
  statement_id  = "AllowAPIGatewayInvokeValidation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.validation.execution_arn}/*/POST/validate"
}

resource "aws_api_gateway_method" "options_validate" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.validate.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options_validate" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  resource_id = aws_api_gateway_resource.validate.id
  http_method = aws_api_gateway_method.options_validate.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\":200}"
  }
}

resource "aws_api_gateway_method_response" "options_200" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  resource_id = aws_api_gateway_resource.validate.id
  http_method = aws_api_gateway_method.options_validate.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true,
    "method.response.header.Access-Control-Allow-Methods" = true,
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options_200" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  resource_id = aws_api_gateway_resource.validate.id
  http_method = aws_api_gateway_method.options_validate.http_method
  status_code = aws_api_gateway_method_response.options_200.status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,Authorization'",
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'",
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
}

resource "aws_api_gateway_stage" "validation" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  deployment_id = aws_api_gateway_deployment.validation.id
  stage_name    = var.env
}

output "validate_endpoint" {
  value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/validate"
}

output "kinesis_stream_name" {
  value = aws_kinesis_stream.events.name
}

output "validation_lambda_name" {
  value = aws_lambda_function.validation.function_name
}

output "feature_lambda_name" {
  value = aws_lambda_function.feature_extraction.function_name
}

# Additional API resources mapping to same validation lambda for different synthetic event types
resource "aws_api_gateway_resource" "telemetry" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  parent_id   = aws_api_gateway_rest_api.validation.root_resource_id
  path_part   = "telemetry"
}
resource "aws_api_gateway_method" "post_telem" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.telemetry.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "post_telem_integration" {
  rest_api_id             = aws_api_gateway_rest_api.validation.id
  resource_id             = aws_api_gateway_resource.telemetry.id
  http_method             = aws_api_gateway_method.post_telem.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.validation.invoke_arn
}
resource "aws_lambda_permission" "apigw_invoke_validation_telem" {
  statement_id  = "AllowAPIGatewayInvokeValidationTelem"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.validation.execution_arn}/*/POST/telemetry"
}

resource "aws_api_gateway_resource" "status" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  parent_id   = aws_api_gateway_rest_api.validation.root_resource_id
  path_part   = "status"
}
resource "aws_api_gateway_method" "post_status" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.status.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "post_status_integration" {
  rest_api_id             = aws_api_gateway_rest_api.validation.id
  resource_id             = aws_api_gateway_resource.status.id
  http_method             = aws_api_gateway_method.post_status.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.validation.invoke_arn
}
resource "aws_lambda_permission" "apigw_invoke_validation_status" {
  statement_id  = "AllowAPIGatewayInvokeValidationStatus"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.validation.execution_arn}/*/POST/status"
}

resource "aws_api_gateway_resource" "location" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  parent_id   = aws_api_gateway_rest_api.validation.root_resource_id
  path_part   = "location"
}
resource "aws_api_gateway_method" "post_location" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.location.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "post_location_integration" {
  rest_api_id             = aws_api_gateway_rest_api.validation.id
  resource_id             = aws_api_gateway_resource.location.id
  http_method             = aws_api_gateway_method.post_location.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.validation.invoke_arn
}
resource "aws_lambda_permission" "apigw_invoke_validation_location" {
  statement_id  = "AllowAPIGatewayInvokeValidationLocation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.validation.execution_arn}/*/POST/location"
}

resource "aws_api_gateway_resource" "trips" {
  rest_api_id = aws_api_gateway_rest_api.validation.id
  parent_id   = aws_api_gateway_rest_api.validation.root_resource_id
  path_part   = "trips"
}
resource "aws_api_gateway_method" "post_trips" {
  rest_api_id   = aws_api_gateway_rest_api.validation.id
  resource_id   = aws_api_gateway_resource.trips.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "post_trips_integration" {
  rest_api_id             = aws_api_gateway_rest_api.validation.id
  resource_id             = aws_api_gateway_resource.trips.id
  http_method             = aws_api_gateway_method.post_trips.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.validation.invoke_arn
}
resource "aws_lambda_permission" "apigw_invoke_validation_trips" {
  statement_id  = "AllowAPIGatewayInvokeValidationTrips"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validation.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.validation.execution_arn}/*/POST/trips"
}

# Update deployment to depend on all integrations so redeploy occurs properly
resource "aws_api_gateway_deployment" "validation" {
  depends_on = [
    aws_api_gateway_integration.post_validate_integration,
    aws_api_gateway_integration.post_telem_integration,
    aws_api_gateway_integration.post_status_integration,
    aws_api_gateway_integration.post_location_integration,
    aws_api_gateway_integration.post_trips_integration
  ]
  rest_api_id = aws_api_gateway_rest_api.validation.id
  triggers = {
    redeploy = sha1(jsonencode({
      body        = aws_api_gateway_rest_api.validation.body
      integrations = [
        aws_api_gateway_integration.post_validate_integration.uri,
        aws_api_gateway_integration.post_telem_integration.uri,
        aws_api_gateway_integration.post_status_integration.uri,
        aws_api_gateway_integration.post_location_integration.uri,
        aws_api_gateway_integration.post_trips_integration.uri
      ]
    }))
  }
}

output "telemetry_endpoint" { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/telemetry" }
output "status_endpoint"    { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/status" }
output "location_endpoint"  { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/location" }
output "trips_endpoint"     { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/trips" }
