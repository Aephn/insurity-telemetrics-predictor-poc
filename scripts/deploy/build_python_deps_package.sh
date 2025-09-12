#!/usr/bin/env bash
set -euo pipefail
# build_python_deps_package.sh (no Docker version)
# Purpose: Install ONLY the specified Python dependencies locally with pip into a work directory
#          and zip them for later Lambda deployment or layering. No source code, no AWS calls.
#
# Steps:
#  1. Clean/create work directory
#  2. pip install deps into it
#  3. Zip directory -> deps_package.zip (or OUT_ZIP)
#  4. Print summary
#
# Usage:
#   ./build_python_deps_package.sh
#   VALIDATION_DEPS="pydantic numpy" OUT_ZIP=mydeps.zip ./build_python_deps_package.sh
#
# Env Vars:
#   VALIDATION_DEPS   Space separated deps (default: pydantic)
#   OUT_ZIP           Output zip (default: deps_package.zip)
#   WORK_DIR          Working dir (default: deps_build_work)
#   PIP_FLAGS         Extra pip flags (optional)
#
# Exit Codes:
#   0 success
#   2 pip install failure
#   3 zip creation failure
#   4 no dependencies specified

DEPS=${VALIDATION_DEPS:-pydantic}
OUT_ZIP=${OUT_ZIP:-deps_package.zip}
WORK_DIR=${WORK_DIR:-deps_build_work}
PIP_FLAGS=${PIP_FLAGS:-"--no-cache-dir"}

if [[ -z "$DEPS" ]]; then
  echo "[error] No dependencies specified (VALIDATION_DEPS empty)" >&2
  exit 4
fi

echo "[deps] Dependencies: $DEPS"
echo "[deps] Work dir: $WORK_DIR"
echo "[deps] Output zip: $OUT_ZIP"

rm -rf "$WORK_DIR" "$OUT_ZIP"
mkdir -p "$WORK_DIR"

echo "[deps] Installing with host pip"
if ! pip install $PIP_FLAGS $DEPS -t "$WORK_DIR"; then
  echo "[error] pip install failed" >&2
  exit 2
fi

echo "[deps] Creating zip"
(
  cd "$WORK_DIR" || exit 3
  zip -qr "../$OUT_ZIP" . || { echo "[error] zip creation failed" >&2; exit 3; }
)

SIZE=$(du -h "$OUT_ZIP" | cut -f1)
FILE_COUNT=$( (cd "$WORK_DIR" && find . -type f | wc -l | tr -d ' ') )
echo "[deps] Created $OUT_ZIP ($SIZE) with $FILE_COUNT files"
echo "[deps] Done. Use this archive when assembling Lambda deployment." 
