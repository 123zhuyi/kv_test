#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/autodl-tmp/cancel-audit}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3-8B}"
PORT="${PORT:-18000}"
RESULT_DIR="${RESULT_DIR:-$ROOT/results/vllm8b_remote/vllm}"

cd "$ROOT"
mkdir -p "$RESULT_DIR"

echo "[vllmab] root=$ROOT"
date -u +"[vllmab] started_utc=%Y-%m-%dT%H:%M:%SZ"
nvidia-smi || true

source .venv-vllm/bin/activate

PKG_ROOT="$(python - <<'PY'
import pathlib, vllm
print(pathlib.Path(vllm.__file__).resolve().parent)
PY
)"
echo "[vllmab] vllm at $PKG_ROOT"
python -c "import vllm; print('[vllmab] vllm', vllm.__version__)"
python "$ROOT/instrument_vllm_028.py" "$PKG_ROOT" --diff-dir "$RESULT_DIR/provenance"

export CANCEL_AUDIT_TRACE=1
export CANCEL_AUDIT_TRACE_FILE="$RESULT_DIR/runtime_trace.jsonl"
export CANCEL_AUDIT_REQUEST_PREFIX="cancel-audit-"
# RTX 5090 (sm120): flashinfer JIT arch check rejects sm120; use native sampler
# (matches the SGLang arm's --sampling-backend pytorch).
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

SERVER_LOG="$RESULT_DIR/server.log"
rm -f "$CANCEL_AUDIT_TRACE_FILE" "$SERVER_LOG"

python -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_DIR" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --trust-remote-code \
  --gpu-memory-utilization 0.60 \
  --max-model-len "${MAX_MODEL_LEN:-8192}" \
  --enforce-eager \
  >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$RESULT_DIR/server.pid"

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

python - <<PY
import time, urllib.request, sys
url = "http://127.0.0.1:${PORT}/health"
deadline = time.time() + 900
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            print("health", r.status)
            sys.exit(0)
    except Exception:
        time.sleep(5)
print("server did not become healthy", file=sys.stderr)
sys.exit(1)
PY

grep -i "kv cache size\|maximum concurrency" "$SERVER_LOG" | head -4 || true

python "$ROOT/run_capacity_loss.py" \
  --runtime vllm \
  --url "http://127.0.0.1:${PORT}/v1/chat/completions" \
  --model "$MODEL_DIR" \
  --requests "${MAIN_REQUESTS:-100}" \
  --calibration-requests "${CALIB_REQUESTS:-60}" \
  --calibration-rates "${CALIB_RATES:-12}" \
  --max-tokens 512 \
  --cancel-after-events 8 \
  --output-dir "$RESULT_DIR"

python "$ROOT/analyze_lifecycle.py" \
  "$RESULT_DIR/vllm/"*.client.jsonl \
  "$CANCEL_AUDIT_TRACE_FILE" \
  --output-dir "$RESULT_DIR/analysis" || true

date -u +"[vllmab] finished_utc=%Y-%m-%dT%H:%M:%SZ"
echo "[vllmab] result_dir=$RESULT_DIR"
