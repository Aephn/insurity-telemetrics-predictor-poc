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
variable "pricing_dependencies" {
  description = "Dependencies to vendor into pricing engine lambda"
  type        = list(string)
  # Pricing lambda only enriches existing SageMaker predictions; no heavy ML libs needed.
  default     = []
}

locals {
  name_prefix = "telemetry-min-${var.env}"
  tags        = { Environment = var.env, Service = "telemetry-min" }
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

output "pricing_lambda_name" { value = aws_lambda_function.pricing_engine.function_name }

output "sagemaker_endpoint_name" {
  value       = var.deploy_sagemaker ? aws_sagemaker_endpoint.xgb_ep[0].name : null
  description = "Deployed SageMaker endpoint name"
}

output "telemetry_endpoint" { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/telemetry" }
output "status_endpoint"    { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/status" }
output "location_endpoint"  { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/location" }
output "trips_endpoint"     { value = "https://${aws_api_gateway_rest_api.validation.id}.execute-api.${var.region}.amazonaws.com/${var.env}/trips" }
