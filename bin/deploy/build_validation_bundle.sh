#!/usr/bin/env bash
set -euo pipefail

# Build and directly upload a validation lambda bundle (code + dependencies).
# This script will:
#  1. Install dependencies (default: pydantic) into a temp work dir
#  2. Copy source code from validation lambda directory
#  3. Zip into validation_lambda_bundle.zip (override with OUT_ZIP)
#  4. Determine target Lambda function name
#       a. Use --function-name arg if provided
#       b. Else use VALIDATION_LAMBDA_NAME env var if set
#       c. Else attempt to parse terraform output (terraform output -raw validation_lambda_name)
#       d. Else fall back to pattern telemetry-min-${ENV:-dev}-validation
#  5. Call aws lambda update-function-code with produced zip
#
# Usage:
#   ./build_validation_bundle.sh [--function-name MyLambda] [--no-upload]
# Environment overrides:
#   VALIDATION_DEPS           Space separated dependency list (default pydantic)
#   VALIDATION_SRC_DIR        Path to lambda source (default ../../src/aws_lambda/validation)
#   OUT_ZIP                   Output zip filename (default validation_lambda_bundle.zip)
#   ENV                       Environment label used in fallback name (default dev)
#   VALIDATION_LAMBDA_NAME    Explicit lambda name (alternative to CLI flag)
#   AWS_PROFILE / AWS_REGION  Standard AWS CLI environment
#   USE_DOCKER=0              Force disable docker even if present
#
# Requirements: aws cli configured with permissions to update lambda code.

DEPS=${VALIDATION_DEPS:-pydantic}
SRC_DIR=${VALIDATION_SRC_DIR:-../../src/aws_lambda/validation}
OUT_ZIP=${OUT_ZIP:-validation_lambda_bundle.zip}
WORK=validation_build_work
RUNTIME_IMAGE=public.ecr.aws/lambda/python:3.11
ENV_LABEL=${ENV:-dev}
NO_UPLOAD=0
CLI_FUNCTION_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --function-name)
      CLI_FUNCTION_NAME="$2"; shift 2 ;;
    --no-upload)
      NO_UPLOAD=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo "[bundle] Dependencies: $DEPS"
echo "[bundle] Source dir: $SRC_DIR"

rm -rf "$WORK" "$OUT_ZIP"
mkdir -p "$WORK"

if [[ ! -d $SRC_DIR ]]; then
  echo "[error] Source directory $SRC_DIR does not exist" >&2
  exit 2
fi

if [[ ${USE_DOCKER:-1} -eq 1 ]] && command -v docker >/dev/null 2>&1; then
  echo "[bundle] Using docker runtime image $RUNTIME_IMAGE"
  # Override the Lambda image entrypoint to run a shell command for installing deps
  docker run --rm \
    --entrypoint /bin/sh \
    -v "$(pwd)/$WORK":/opt/build \
    -w /opt/build \
    "$RUNTIME_IMAGE" \
    -c "python3.11 -m pip install --no-cache-dir $DEPS -t ."
else
  echo "[bundle] Using host pip (wheel compatibility not guaranteed for Lambda)" >&2
  pip install --no-cache-dir $DEPS -t "$WORK"
fi

cp -R "$SRC_DIR"/* "$WORK"/

( cd "$WORK" && zip -qr "../$OUT_ZIP" . )

echo "[bundle] Created zip: $(du -h "$OUT_ZIP" | cut -f1) $OUT_ZIP"

resolve_lambda_name() {
  if [[ -n $CLI_FUNCTION_NAME ]]; then echo "$CLI_FUNCTION_NAME"; return; fi
  if [[ -n ${VALIDATION_LAMBDA_NAME:-} ]]; then echo "$VALIDATION_LAMBDA_NAME"; return; fi
  if command -v terraform >/dev/null 2>&1 && [[ -f terraform.tfstate || -d .terraform ]]; then
    if terraform output -raw validation_lambda_name >/dev/null 2>&1; then
      terraform output -raw validation_lambda_name && return
    fi
  fi
  echo "telemetry-min-${ENV_LABEL}-validation"
}

TARGET_NAME=$(resolve_lambda_name)
echo "[bundle] Target Lambda function: $TARGET_NAME"

if [[ $NO_UPLOAD -eq 1 ]]; then
  echo "[bundle] Skipping upload (--no-upload set)."; exit 0
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "[error] aws CLI not found in PATH" >&2
  exit 3
fi

echo "[upload] Updating Lambda function code..."
set +e
UPDATE_JSON=$(aws lambda update-function-code --function-name "$TARGET_NAME" --zip-file "fileb://$OUT_ZIP" 2>&1)
STATUS=$?
set -e
if [[ $STATUS -ne 0 ]]; then
  echo "[error] aws cli update failed:" >&2
  echo "$UPDATE_JSON" >&2
  exit $STATUS
fi
echo "$UPDATE_JSON" | grep -E 'FunctionName|LastModified|Version' || echo "$UPDATE_JSON"
echo "[done] Lambda code updated for $TARGET_NAME"
