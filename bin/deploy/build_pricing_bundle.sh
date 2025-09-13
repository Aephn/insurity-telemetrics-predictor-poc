#!/usr/bin/env bash
set -euo pipefail

# Build a pricing engine lambda bundle (code + dependencies) similar to validation bundle script.
# Steps:
#  1. Install dependencies into temp dir (default: numpy pandas xgboost)
#  2. Copy pricing_engine source code
#  3. (Optional) copy minimal model artifacts (xgb_model.json + feature_pipeline.joblib + meta.json) if present
#  4. Zip into pricing_lambda_bundle.zip (override with OUT_ZIP)
#  5. Optionally upload to existing lambda unless --no-upload
#
# Environment overrides:
#   PRICING_DEPS            Space separated dependency list (terraform default now: "numpy pandas")
#   PRICING_SRC_DIR         Source directory (default ../../src/aws_lambda/pricing_engine)
#   MODEL_ARTIFACTS_DIR     Directory containing model artifacts to embed (default ../../artifacts)
#   OUT_ZIP                 Output zip filename (default pricing_lambda_bundle.zip)
#   ENV                     Environment label (default dev)
#   PRICING_LAMBDA_NAME     Explicit lambda name
#   USE_DOCKER=1            Use docker lambda python image for building deps
#
# Usage:
#   ./build_pricing_bundle.sh [--function-name MyLambda] [--no-upload]

DEPS=${PRICING_DEPS:-"numpy pandas"}
SRC_DIR=${PRICING_SRC_DIR:-../../src/aws_lambda/pricing_engine}
MODEL_DIR=${MODEL_ARTIFACTS_DIR:-../../artifacts}
OUT_ZIP=${OUT_ZIP:-pricing_lambda_bundle.zip}
WORK=pricing_build_work
RUNTIME_IMAGE=public.ecr.aws/lambda/python:3.11
ENV_LABEL=${ENV:-dev}
NO_UPLOAD=0
CLI_FUNCTION_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --function-name) CLI_FUNCTION_NAME="$2"; shift 2 ;;
    --no-upload) NO_UPLOAD=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo "[pricing] Dependencies: ${DEPS:-<none>}"
echo "[pricing] Source dir: $SRC_DIR"

rm -rf "$WORK" "$OUT_ZIP"
mkdir -p "$WORK"

if [[ ! -d $SRC_DIR ]]; then
  echo "[error] Source directory $SRC_DIR does not exist" >&2
  exit 2
fi

if [[ -n "$DEPS" ]]; then
  if [[ ${USE_DOCKER:-1} -eq 1 ]] && command -v docker >/dev/null 2>&1; then
    echo "[pricing] Using docker runtime image $RUNTIME_IMAGE"
    docker run --rm \
      --entrypoint /bin/sh \
      -v "$(pwd)/$WORK":/opt/build \
      -w /opt/build \
      "$RUNTIME_IMAGE" \
      -c "set -e; (python3.11 -m pip install --no-cache-dir --only-binary=:all: $DEPS -t . || python3.11 -m pip install --no-cache-dir $DEPS -t .); python3.11 - <<'PY'
import importlib, sys
missing=[]
for m in ('numpy','pandas','pandas._config'):
  try: importlib.import_module(m)
  except Exception as e: missing.append(f'{m}:{e}')
if missing:
  print('[verify] Missing modules:', missing, file=sys.stderr); sys.exit(1)
print('[verify] Core modules present')
PY"
  else
    echo "[pricing] Using host pip (may produce incompatible wheels)" >&2
    pip install --no-cache-dir $DEPS -t "$WORK"
  fi
else
  echo "[pricing] No dependencies specified; building code-only zip"
fi

cp -R "$SRC_DIR"/* "$WORK"/

# Embed minimal model artifacts if present
if [[ -f $MODEL_DIR/xgb_model.json ]]; then
  mkdir -p "$WORK/artifacts"
  cp -f $MODEL_DIR/xgb_model.json "$WORK/artifacts/" || true
fi
if [[ -f $MODEL_DIR/feature_pipeline.joblib ]]; then
  mkdir -p "$WORK/artifacts"
  cp -f $MODEL_DIR/feature_pipeline.joblib "$WORK/artifacts/" || true
fi
if [[ -f $MODEL_DIR/meta.json ]]; then
  mkdir -p "$WORK/artifacts"
  cp -f $MODEL_DIR/meta.json "$WORK/artifacts/" || true
fi

echo "[pricing] Model artifacts embedded:"; ls -1 "$WORK/artifacts" 2>/dev/null || echo "(none)"

( cd "$WORK" && zip -qr "../$OUT_ZIP" . )

echo "[pricing] Created zip: $(du -h "$OUT_ZIP" | cut -f1) $OUT_ZIP"

resolve_lambda_name() {
  if [[ -n $CLI_FUNCTION_NAME ]]; then echo "$CLI_FUNCTION_NAME"; return; fi
  if [[ -n ${PRICING_LAMBDA_NAME:-} ]]; then echo "$PRICING_LAMBDA_NAME"; return; fi
  if command -v terraform >/dev/null 2>&1 && [[ -f terraform.tfstate || -d .terraform ]]; then
    if terraform output -raw pricing_lambda_name >/dev/null 2>&1; then
      terraform output -raw pricing_lambda_name && return
    fi
  fi
  echo "telemetry-min-${ENV_LABEL}-pricing"
}

TARGET_NAME=$(resolve_lambda_name)

if [[ $NO_UPLOAD -eq 1 ]]; then
  echo "[pricing] Skipping upload (--no-upload set). Target would be $TARGET_NAME"; exit 0
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "[error] aws CLI not found" >&2; exit 3
fi

echo "[upload] Updating Lambda function code for $TARGET_NAME"
set +e
UPDATE_JSON=$(aws lambda update-function-code --function-name "$TARGET_NAME" --zip-file "fileb://$OUT_ZIP" 2>&1)
STATUS=$?
set -e
if [[ $STATUS -ne 0 ]]; then
  echo "[error] aws update failed:" >&2
  echo "$UPDATE_JSON" >&2
  exit $STATUS
fi
echo "$UPDATE_JSON" | grep -E 'FunctionName|LastModified|Version' || echo "$UPDATE_JSON"
echo "[done] Lambda code updated for $TARGET_NAME"
