#!/usr/bin/env bash
# Fetch the quantized Qwen routing-judge model onto the slice EC2 box.
#
# The llama.cpp "judge" service (infra/ec2/files/docker-compose.yml) mounts
# /opt/slice/models read-only and loads /models/judge_q4.gguf. This pulls that one
# file (about 379MB, Qwen2.5-0.5B Q4_K_M) from the private judge-weights S3 bucket,
# which the instance role can already read, using the host aws CLI.
#
# The bucket name is passed in as the first argument or the JUDGE_WEIGHTS_BUCKET env
# var (set in user_data from Terraform), so no bucket name is baked into this
# committed script. JUDGE_MODELS_DIR overrides the destination dir (default
# /opt/slice/models), which keeps the script testable off the box.
#
# Idempotent and safe to run twice: if the model file is already present and nonempty
# it downloads nothing and exits 0, so it can run on every boot. Fails loud on error.
set -euo pipefail

BUCKET="${1:-${JUDGE_WEIGHTS_BUCKET:-}}"
MODELS_DIR="${JUDGE_MODELS_DIR:-/opt/slice/models}"
KEY="judge_q4.gguf"
DEST="$MODELS_DIR/$KEY"

: "${BUCKET:?bucket name required (first argument or JUDGE_WEIGHTS_BUCKET)}"

# Already have it? Do nothing. -s is true only for an existing, nonempty file, so a
# half-written or empty file from a prior failed run is retried rather than kept.
if [ -s "$DEST" ]; then
  echo "judge model already present at $DEST; skipping download"
  exit 0
fi

mkdir -p "$MODELS_DIR"
echo "downloading s3://$BUCKET/$KEY -> $DEST"
aws s3 cp "s3://$BUCKET/$KEY" "$DEST"
echo "judge model ready at $DEST"
