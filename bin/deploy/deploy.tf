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
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0"
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
variable "python_executable" {
  description = "Python executable to use for dependency installation"
  type        = string
  default     = "python3"
}
variable "validation_dependencies" {
  description = "Python packages to vendor directly into the validation lambda zip (built via external script)"
  type        = list(string)
  default     = ["pydantic"]
}
variable "feature_source_dir" {
  type    = string
  default = "../../src/aws_lambda/feature_extraction"
}
variable "kinesis_shard_count" {
  type    = number
  default = 1
}
variable "model_artifacts_dir" {
  description = "Local directory containing trained model artifacts (xgb_model.json, feature_pipeline.joblib, meta.json). If missing, we will synthesize by running local training script."
  type        = string
  default     = "../../artifacts"
}
variable "sagemaker_model_name" {
  description = "Name for the SageMaker model and endpoint base."
  type        = string
  default     = "telemetry-xgb-model"
}
variable "deploy_sagemaker" {
  description = "Whether to create SageMaker model + endpoint (bool)."
  type        = bool
  default     = true
}
variable "sagemaker_serverless_memory_mb" {
  description = "Memory size (MB) for SageMaker serverless endpoint (must be within account quota)."
  type        = number
  default     = 3072
}
variable "model_packaging_revision" {
  description = "Increment to force rebuild of model artifacts tarball when packaging logic changes."
  type        = number
  default     = 2
}
variable "telemetry_table_name" {
  description = "Name of existing single-table DynamoDB (from databases.tf) to persist model predictions into."
  type        = string
  default     = "TelemetryUserData"
}
variable "pricing_source_dir" {
  description = "Path to pricing engine lambda source"
  type        = string
  default     = "../../src/aws_lambda/pricing_engine"
}
## dashboard_source_dir no longer needed (dashboard reuses pricing bundle)
variable "pricing_dependencies" {
  description = "Dependencies to vendor into pricing engine lambda"
  type        = list(string)
  # Pin versions to ensure manylinux wheels (avoid source build inside lambda base image lacking compilers)
  default     = ["numpy==2.2.6", "pandas==2.3.2"]
}
variable "price_history_bucket_name" {
  description = "S3 bucket name for price history exports"
  type        = string
  default     = "telemetry-price-history"
}


locals {
  name_prefix = "telemetry-min-${var.env}"
  tags        = { Environment = var.env, Service = "telemetry-min" }
}

#############################################
# Storage Layer (merged from databases.tf)
#############################################

resource "aws_dynamodb_table" "telemetry" {
  name         = var.telemetry_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }
  attribute {
    name = "GSI2PK"
    type = "S"
  }
  attribute {
    name = "GSI2SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1_EventsByUser"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "INCLUDE"
    non_key_attributes = ["event_type", "severity", "value", "speedMph"]
  }

  global_secondary_index {
    name            = "GSI2_PeriodAggregates"
    hash_key        = "GSI2PK"
    range_key       = "GSI2SK"
    projection_type = "INCLUDE"
    non_key_attributes = ["risk_score", "final_monthly_premium", "safety_score"]
  }

  point_in_time_recovery {
    enabled = true
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  tags = merge(local.tags, { Store = "dynamodb" })
}

resource "aws_s3_bucket" "price_history" {
  bucket        = var.price_history_bucket_name
  force_destroy = var.env != "prod"
  tags          = merge(local.tags, { Purpose = "price-history" })
}

resource "aws_s3_bucket_versioning" "price_history" {
  bucket = aws_s3_bucket.price_history.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "price_history" {
  bucket = aws_s3_bucket.price_history.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "price_history" {
  bucket                  = aws_s3_bucket.price_history.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "price_history" {
  bucket = aws_s3_bucket.price_history.id
  rule {
    id     = "cold-tier"
    status = "Enabled"
    transition {
      days          = 30
      storage_class = "INTELLIGENT_TIERING"
    }
    transition {
      days          = 180
      storage_class = "DEEP_ARCHIVE"
    }
    noncurrent_version_transition {
      noncurrent_days = 60
      storage_class   = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = 730
    }
  }
}

# Build validation lambda deployment package including dependencies at apply time.
# We compute a plan-safe change key from source files + dependency list, and use
# a local-exec to produce the zip before Lambda code upload.
locals {
  validation_src_files = fileset(var.validation_source_dir, "**")
  validation_src_hash  = sha256(join("", [for f in local.validation_src_files : filesha256("${var.validation_source_dir}/${f}")]))
  validation_deps_key  = join(",", var.validation_dependencies)
  validation_code_change_key = base64sha256("${local.validation_src_hash}|${local.validation_deps_key}")
  validation_bundle_path = "${path.module}/validation_lambda_bundle.zip"
}

resource "null_resource" "validation_build" {
  # Rebuild bundle when source or deps change
  triggers = {
    code = local.validation_code_change_key
  }

  provisioner "local-exec" {
    working_dir = path.module
  command     = "VALIDATION_DEPS=\"${join(" ", var.validation_dependencies)}\" USE_DOCKER=1 ./build_validation_bundle.sh --no-upload"
    interpreter = ["/bin/bash", "-lc"]
  }
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

data "archive_file" "feature_zip" {
  type        = "zip"
  source_dir  = var.feature_source_dir
  output_path = "feature_lambda.zip"
}

## (feature zip data block relocated above)

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

## (feature inference permissions moved lower after caller identity data block for account id)

resource "aws_lambda_function" "validation" {
  function_name = "${local.name_prefix}-validation"
  role          = aws_iam_role.validation_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  filename         = local.validation_bundle_path
  # Plan-safe content key derived from source + deps; triggers updates without
  # requiring the zip to exist at plan time.
  source_code_hash = local.validation_code_change_key
  timeout       = 10
  memory_size   = 256
  architectures = ["arm64"]
  environment {
    variables = {
      KINESIS_STREAM_NAME = aws_kinesis_stream.events.name
      ENV                 = var.env
    }
  }
  depends_on = [
    aws_iam_role_policy_attachment.validation_basic,
    aws_iam_role_policy.validation_kinesis_write,
    null_resource.validation_build
  ]
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
  SAGEMAKER_ENDPOINT_NAME = var.deploy_sagemaker ? aws_sagemaker_endpoint.xgb_ep[0].name : ""
  TELEMETRY_TABLE_NAME    = var.telemetry_table_name
  PRICING_LAMBDA_NAME     = aws_lambda_function.pricing_engine.function_name
  MIN_EXPOSURE_MILES      = "0.0"
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

# ------------------------ SageMaker Model Deployment (Optional) ------------------------

resource "random_id" "model_bucket_suffix" {
  byte_length = 4
  keepers = {
    env = var.env
  }
}

resource "aws_s3_bucket" "model_artifacts" {
  count = var.deploy_sagemaker ? 1 : 0
  bucket = "${local.name_prefix}-model-${random_id.model_bucket_suffix.hex}"
  force_destroy = true
  tags = local.tags
}

locals {
  model_artifact_source_files = fileset(var.model_artifacts_dir, "**")
  model_artifact_hash = length(local.model_artifact_source_files) > 0 ? sha256(join("", [for f in local.model_artifact_source_files : filesha256("${var.model_artifacts_dir}/${f}")])) : "empty"
  model_package_key = "model_artifacts_${local.model_artifact_hash}.tar.gz"
}

resource "null_resource" "package_model" {
  count = var.deploy_sagemaker ? 1 : 0
  triggers = {
  src_hash = local.model_artifact_hash
  rev      = var.model_packaging_revision
  }
  provisioner "local-exec" {
    when        = create
    working_dir = path.module
    # If artifacts dir is empty, run local training to create them, then tarball.
    command = <<EOT
      set -euo pipefail
      ART_DIR="${var.model_artifacts_dir}"
      if [ ! -d "$ART_DIR" ] || [ -z "$(ls -A "$ART_DIR" 2>/dev/null || true)" ]; then
        echo "[model] Artifacts missing; generating via local training run"
        ${var.python_executable} ../../models/aws_sagemaker/xgboost_model.py --local-train --model-dir "$ART_DIR"
      fi
      echo "[model] Original artifact directory listing:" 
      ls -l "$ART_DIR"
      # Expect training script to have produced xgboost-model already
      if [ ! -f "$ART_DIR/xgboost-model" ]; then
        echo "[model][ERROR] xgboost-model missing; re-run training to generate artifacts" >&2; exit 1
      fi
      STAGE_DIR=$(mktemp -d stage_booster_XXXX)
      cp "$ART_DIR/xgboost-model" "$STAGE_DIR/"
      echo "[model] Staged minimal contents:"; ls -l "$STAGE_DIR"
      tar -C "$STAGE_DIR" -czf model_artifacts.tar.gz .
      echo "[model] Created minimal model_artifacts.tar.gz (size: $(du -h model_artifacts.tar.gz | cut -f1))"
EOT
    interpreter = ["/bin/bash", "-lc"]
  }
}

resource "aws_s3_object" "model_package" {
  count  = var.deploy_sagemaker ? 1 : 0
  bucket = aws_s3_bucket.model_artifacts[0].id
  key    = local.model_package_key
  source = "${path.module}/model_artifacts.tar.gz"
  depends_on = [null_resource.package_model]
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "sagemaker_execution" {
  count = var.deploy_sagemaker ? 1 : 0
  name  = "${local.name_prefix}-sagemaker-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "sagemaker.amazonaws.com" },
      Action   = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "sagemaker_access" {
  count = var.deploy_sagemaker ? 1 : 0
  name  = "${local.name_prefix}-sagemaker-access"
  role  = aws_iam_role.sagemaker_execution[0].id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.model_artifacts[0].arn}/*"] },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
    ]
  })
}

# Obtain latest XGBoost container URI (region specific). For prototype we hardcode a common version; adjust if unsupported.
locals {
  xgboost_image_version = "1.7-1"
  # Use configured region variable to avoid deprecated aws_region.name attribute.
  xgboost_repository    = "683313688378.dkr.ecr.${var.region}.amazonaws.com/sagemaker-xgboost:${local.xgboost_image_version}"
}

resource "aws_sagemaker_model" "xgb" {
  count               = var.deploy_sagemaker ? 1 : 0
  name                = var.sagemaker_model_name
  execution_role_arn  = aws_iam_role.sagemaker_execution[0].arn
  primary_container {
    image          = local.xgboost_repository
    mode           = "SingleModel"
    model_data_url = "s3://${aws_s3_bucket.model_artifacts[0].bucket}/${aws_s3_object.model_package[0].key}"
    environment = {
      "PREMIUM_SCALING_TARGET_SPREAD" = "0.35"
    }
  }
  depends_on = [aws_s3_object.model_package]
  tags       = local.tags
}

resource "aws_sagemaker_endpoint_configuration" "xgb_cfg" {
  count = var.deploy_sagemaker ? 1 : 0
  name  = "${var.sagemaker_model_name}-cfg"
  production_variants {
    model_name    = aws_sagemaker_model.xgb[0].name
    variant_name  = "AllTraffic"
    serverless_config {
      max_concurrency     = 2
  memory_size_in_mb   = var.sagemaker_serverless_memory_mb
    }
  }
  tags = local.tags
}

resource "aws_sagemaker_endpoint" "xgb_ep" {
  count                = var.deploy_sagemaker ? 1 : 0
  name                 = "${var.sagemaker_model_name}-ep"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.xgb_cfg[0].name
  tags                 = local.tags
}

# Re-introduced here so we can (optionally) reference the endpoint ARN above
resource "aws_iam_role_policy" "feature_inference_permissions" {
  name = "${local.name_prefix}-feature-inference"
  role = aws_iam_role.feature_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      for s in concat(
        var.deploy_sagemaker ? [jsonencode({
          Effect   = "Allow"
          Action   = ["sagemaker:InvokeEndpoint"]
          Resource = aws_sagemaker_endpoint.xgb_ep[0].arn
        })] : [],
        [jsonencode({
          Effect   = "Allow"
          Action   = ["dynamodb:PutItem"]
          Resource = "arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.telemetry_table_name}"
  })],
  [jsonencode({
          Effect = "Allow"
          Action = ["lambda:InvokeFunction"]
          Resource = aws_lambda_function.pricing_engine.arn
        })]
      ) : jsondecode(s)
    ]
  })
}

# ---------------- Pricing Engine Lambda (post-SageMaker enrichment) ----------------

locals {
  pricing_src_files = fileset(var.pricing_source_dir, "**")
  pricing_src_hash  = sha256(join("", [for f in local.pricing_src_files : filesha256("${var.pricing_source_dir}/${f}")]))
  pricing_deps_key  = join(",", var.pricing_dependencies)
  pricing_code_change_key = base64sha256("${local.pricing_src_hash}|${local.pricing_deps_key}")
  pricing_bundle_path = "${path.module}/pricing_lambda_bundle.zip"
}

resource "null_resource" "pricing_build" {
  triggers = { code = local.pricing_code_change_key }
  provisioner "local-exec" {
    working_dir = path.module
    command     = "PRICING_DEPS=\"${join(" ", var.pricing_dependencies)}\" PRICING_SRC_DIR=${var.pricing_source_dir} USE_DOCKER=1 ./build_pricing_bundle.sh --no-upload"
    interpreter = ["/bin/bash", "-lc"]
  }
}

resource "aws_iam_role" "pricing_role" {
  name = "${local.name_prefix}-pricing-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "pricing_basic" {
  role       = aws_iam_role.pricing_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "pricing_engine" {
  function_name = "${local.name_prefix}-pricing"
  role          = aws_iam_role.pricing_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  filename         = local.pricing_bundle_path
  source_code_hash = local.pricing_code_change_key
  timeout       = 15
  memory_size   = 512
  architectures = ["arm64"]
  environment { variables = { ENV = var.env, MODEL_ARTIFACTS_DIR = "/var/task/artifacts" } }
  depends_on = [null_resource.pricing_build, aws_iam_role_policy_attachment.pricing_basic]
  tags = local.tags
}

resource "aws_iam_role" "dashboard_role" {
  name = "${local.name_prefix}-dashboard-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "dashboard_basic" {
  role       = aws_iam_role.dashboard_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "dashboard_ddb_read" {
  name = "${local.name_prefix}-dashboard-ddb-read"
  role = aws_iam_role.dashboard_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
  Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"],
        Resource = ["arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.telemetry_table_name}", "arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.telemetry_table_name}/index/*"]
      }
    ]
  })
}

resource "aws_lambda_function" "dashboard_snapshot" {
  function_name = "${local.name_prefix}-dashboard-snapshot"
  role          = aws_iam_role.dashboard_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  # Reuse pricing bundle (contains numpy+pandas and model artifacts) for snapshot lambda
  filename         = local.pricing_bundle_path
  source_code_hash = local.pricing_code_change_key
  timeout       = 20
  memory_size   = 512
  architectures = ["arm64"]
  environment { variables = { ENV = var.env, MODEL_ARTIFACTS_DIR = "/var/task/artifacts", TELEMETRY_TABLE = var.telemetry_table_name } }
  depends_on = [null_resource.pricing_build, aws_iam_role_policy_attachment.dashboard_basic, aws_iam_role_policy.dashboard_ddb_read]
  tags = local.tags
}

# ---------------- Dashboard API Gateway ----------------

resource "aws_api_gateway_rest_api" "dashboard" {
  name        = "${local.name_prefix}-dashboard-api"
  description = "Frontend dashboard data API"
}

resource "aws_api_gateway_resource" "dashboard_root" {
  rest_api_id = aws_api_gateway_rest_api.dashboard.id
  parent_id   = aws_api_gateway_rest_api.dashboard.root_resource_id
  path_part   = "dashboard"
}

resource "aws_api_gateway_method" "get_dashboard" {
  rest_api_id   = aws_api_gateway_rest_api.dashboard.id
  resource_id   = aws_api_gateway_resource.dashboard_root.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_dashboard_integration" {
  rest_api_id             = aws_api_gateway_rest_api.dashboard.id
  resource_id             = aws_api_gateway_resource.dashboard_root.id
  http_method             = aws_api_gateway_method.get_dashboard.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.dashboard_snapshot.invoke_arn
}

resource "aws_api_gateway_resource" "healthz" {
  rest_api_id = aws_api_gateway_rest_api.dashboard.id
  parent_id   = aws_api_gateway_rest_api.dashboard.root_resource_id
  path_part   = "healthz"
}

resource "aws_api_gateway_method" "get_healthz" {
  rest_api_id   = aws_api_gateway_rest_api.dashboard.id
  resource_id   = aws_api_gateway_resource.healthz.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_healthz_integration" {
  rest_api_id             = aws_api_gateway_rest_api.dashboard.id
  resource_id             = aws_api_gateway_resource.healthz.id
  http_method             = aws_api_gateway_method.get_healthz.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.dashboard_snapshot.invoke_arn
}

resource "aws_lambda_permission" "apigw_invoke_dashboard" {
  statement_id  = "AllowAPIGatewayInvokeDashboard"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dashboard_snapshot.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.dashboard.execution_arn}/*/GET/dashboard"
}

resource "aws_lambda_permission" "apigw_invoke_dashboard_healthz" {
  statement_id  = "AllowAPIGatewayInvokeDashboardHealthz"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dashboard_snapshot.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.dashboard.execution_arn}/*/GET/healthz"
}

resource "aws_api_gateway_deployment" "dashboard" {
  depends_on = [
    aws_api_gateway_integration.get_dashboard_integration,
    aws_api_gateway_integration.get_healthz_integration
  ]
  rest_api_id = aws_api_gateway_rest_api.dashboard.id
  triggers = {
    redeploy = sha1(jsonencode({
      integrations = [
        aws_api_gateway_integration.get_dashboard_integration.uri,
        aws_api_gateway_integration.get_healthz_integration.uri,
      ]
    }))
  }
}

resource "aws_api_gateway_stage" "dashboard" {
  rest_api_id   = aws_api_gateway_rest_api.dashboard.id
  deployment_id = aws_api_gateway_deployment.dashboard.id
  stage_name    = var.env
}

output "dashboard_api_base" { value = "https://${aws_api_gateway_rest_api.dashboard.id}.execute-api.${var.region}.amazonaws.com/${var.env}" }
output "dashboard_endpoint" { value = "https://${aws_api_gateway_rest_api.dashboard.id}.execute-api.${var.region}.amazonaws.com/${var.env}/dashboard" }
output "dashboard_healthz"  { value = "https://${aws_api_gateway_rest_api.dashboard.id}.execute-api.${var.region}.amazonaws.com/${var.env}/healthz" }

output "pricing_lambda_name" { value = aws_lambda_function.pricing_engine.function_name }
output "dynamodb_table_name" { value = aws_dynamodb_table.telemetry.name }
output "price_history_bucket_name" { value = aws_s3_bucket.price_history.bucket }

output "sagemaker_endpoint_name" {
  value       = var.deploy_sagemaker ? aws_sagemaker_endpoint.xgb_ep[0].name : null
  description = "Deployed SageMaker endpoint name"
}

output "telemetry_endpoint" { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/telemetry" }
output "status_endpoint"    { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/status" }
output "location_endpoint"  { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/location" }
output "trips_endpoint"     { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/trips" }
