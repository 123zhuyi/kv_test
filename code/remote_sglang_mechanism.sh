#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/autodl-tmp/cancel-audit}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3-0.6B}"
PORT="${PORT:-18000}"
RESULT_DIR="${RESULT_DIR:-$ROOT/results/mechanism_remote/sglang}"

cd "$ROOT"
mkdir -p "$RESULT_DIR"

echo "[mechanism] root=$ROOT"
date -u +"[mechanism] started_utc=%Y-%m-%dT%H:%M:%SZ"

source .venv/bin/activate

VENV_SITE="$(python - <<'PY'
import site
print(site.getsitepackages()[0])
PY
)"
CUDA_WHEEL_HOME="$VENV_SITE/nvidia/cu13"
if [ -x "$CUDA_WHEEL_HOME/bin/nvcc" ]; then
  export CUDA_HOME="$CUDA_WHEEL_HOME"
  export CUDA_PATH="$CUDA_WHEEL_HOME"
  export PATH="$CUDA_WHEEL_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$CUDA_WHEEL_HOME/lib:$CUDA_WHEEL_HOME/lib64:${LD_LIBRARY_PATH:-}"
  export LIBRARY_PATH="$CUDA_WHEEL_HOME/lib:$CUDA_WHEEL_HOME/lib64:${LIBRARY_PATH:-}"
fi

PKG_ROOT="$(python - <<'PY'
import pathlib, sglang
print(pathlib.Path(sglang.__file__).resolve().parent)
PY
)"
python "$ROOT/instrument_sglang_0519.py" "$PKG_ROOT" --diff-dir "$RESULT_DIR/provenance"

export CANCEL_AUDIT_TRACE=1
export CANCEL_AUDIT_TRACE_FILE="$RESULT_DIR/runtime_trace.jsonl"
export CANCEL_AUDIT_REQUEST_PREFIX="cancel-audit-"

SERVER_LOG="$RESULT_DIR/server.log"
rm -f "$CANCEL_AUDIT_TRACE_FILE" "$SERVER_LOG"

python -m sglang.launch_server \
  --model-path "$MODEL_DIR" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --trust-remote-code \
  --attention-backend triton \
  --sampling-backend pytorch \
  --disable-cuda-graph \
  --mem-fraction-static 0.60 \
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

# 16 requests at 2/s, half FIN-cancelled after 8 SSE events, max_tokens=512.
python "$ROOT/cancel_probe.py" \
  --runtime sglang \
  --url "http://127.0.0.1:${PORT}/v1/chat/completions" \
  --model "$MODEL_DIR" \
  --requests 16 \
  --request-rate 2.0 \
  --cancel-rate 0.5 \
  --cancel-mode fin \
  --cancel-after-events 8 \
  --max-tokens 512 \
  --generation-seed 20260913 \
  --seed 777 \
  --prompt "Write an unbroken numbered list from 1 upward. Do not stop, summarize, or emit an ending sentence." \
  --output "$RESULT_DIR/mechanism.csv"

echo "[mechanism] trace event counts:"
grep -o '"event":"[A-Z_]*"' "$CANCEL_AUDIT_TRACE_FILE" | sort | uniq -c

echo "[mechanism] per-cancelled-request mechanism sequence:"
python - <<'PY'
import json, collections
rows = [json.loads(l) for l in open("/root/autodl-tmp/cancel-audit/results/mechanism_remote/sglang/runtime_trace.jsonl")]
by_rid = collections.defaultdict(list)
for r in rows:
    if r["event"] in ("SERVER_DISCONNECT_DETECTED", "REQ_STATE_DISCARDED", "ABORT_DROPPED_NO_STATE",
                      "ABORT_EMITTED", "SCHEDULER_ABORT_SEEN", "KV_RELEASED"):
        by_rid[r["runtime_request_id"]].append((r["ts_monotonic_ns"], r["event"], r.get("extra", {})))
for rid, evs in sorted(by_rid.items()):
    evs.sort()
    t0 = evs[0][0]
    print(rid)
    for ts, ev, extra in evs:
        print(f"  +{(ts - t0) / 1e6:9.2f} ms  {ev}  {extra if extra else ''}")
PY

date -u +"[mechanism] finished_utc=%Y-%m-%dT%H:%M:%SZ"
echo "[mechanism] result_dir=$RESULT_DIR"
