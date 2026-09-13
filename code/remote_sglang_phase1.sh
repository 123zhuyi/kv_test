#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/autodl-tmp/cancel-audit}"
MODEL="${MODEL:-Qwen/Qwen3-0.6B}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3-0.6B}"
PORT="${PORT:-18000}"
RESULT_DIR="${RESULT_DIR:-$ROOT/results/phase1_remote/sglang}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

mkdir -p "$ROOT" "$RESULT_DIR" "$(dirname "$MODEL_DIR")"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [ -z "$PYTHON_BIN" ] && [ -x /root/miniconda3/bin/python3 ]; then
  PYTHON_BIN=/root/miniconda3/bin/python3
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "python interpreter not found" >&2
  exit 1
fi
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [ -z "$UV_BIN" ] && [ -x /root/miniconda3/bin/uv ]; then
  UV_BIN=/root/miniconda3/bin/uv
fi

echo "[phase1] root=$ROOT"
date -u +"[phase1] started_utc=%Y-%m-%dT%H:%M:%SZ"
nvidia-smi || true
"$PYTHON_BIN" --version

if [ -z "$UV_BIN" ]; then
  "$PYTHON_BIN" -m pip install -U uv -i http://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com
  UV_BIN="$(dirname "$PYTHON_BIN")/uv"
fi

if [ ! -d .venv ]; then
  "$UV_BIN" venv --python "$PYTHON_BIN" .venv
fi
source .venv/bin/activate
VENV_SITE="$(python - <<'PY'
import site
print(site.getsitepackages()[0])
PY
)"
CUDA_WHEEL_HOME="$VENV_SITE/nvidia/cu13"
if [ -x "$CUDA_WHEEL_HOME/bin/nvcc" ]; then
  if [ -d "$CUDA_WHEEL_HOME/lib" ] && [ ! -e "$CUDA_WHEEL_HOME/lib64" ]; then
    ln -s "$CUDA_WHEEL_HOME/lib" "$CUDA_WHEEL_HOME/lib64"
  fi
  if [ -f "$CUDA_WHEEL_HOME/lib/libcudart.so.13" ] && [ ! -e "$CUDA_WHEEL_HOME/lib/libcudart.so" ]; then
    ln -s "$CUDA_WHEEL_HOME/lib/libcudart.so.13" "$CUDA_WHEEL_HOME/lib/libcudart.so"
  fi
  export CUDA_HOME="$CUDA_WHEEL_HOME"
  export CUDA_PATH="$CUDA_WHEEL_HOME"
  export PATH="$CUDA_WHEEL_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$CUDA_WHEEL_HOME/lib:$CUDA_WHEEL_HOME/lib64:${LD_LIBRARY_PATH:-}"
  export LIBRARY_PATH="$CUDA_WHEEL_HOME/lib:$CUDA_WHEEL_HOME/lib64:${LIBRARY_PATH:-}"
fi

"$UV_BIN" pip install --python .venv/bin/python -U pip wheel setuptools -i http://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com
"$UV_BIN" pip install --python .venv/bin/python -U "sglang[all]" transformers huggingface_hub -i http://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com

python - <<'PY'
import importlib.util
for name in ["torch", "sglang", "transformers"]:
    spec = importlib.util.find_spec(name)
    print(f"{name}: {spec.origin if spec else 'missing'}")
PY

export HF_ENDPOINT
export HF_HUB_DISABLE_XET=1
if [ ! -f "$MODEL_DIR/config.json" ]; then
  hf download "$MODEL" --local-dir "$MODEL_DIR"
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
rm -rf /root/.cache/sglang/jit/sm120a /root/.cache/sglang/jit/sm120f || true

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
    except Exception as exc:
        time.sleep(5)
print("server did not become healthy", file=sys.stderr)
sys.exit(1)
PY

python "$ROOT/run_phase1_direct.py" \
  --runtime sglang \
  --url "http://127.0.0.1:${PORT}/v1/chat/completions" \
  --model "$MODEL_DIR" \
  --trials 5 \
  --max-tokens 1024 \
  --output-dir "$RESULT_DIR"

python "$ROOT/analyze_lifecycle.py" \
  "$RESULT_DIR/sglang/*.client.jsonl" \
  "$CANCEL_AUDIT_TRACE_FILE" \
  --output-dir "$RESULT_DIR/analysis" || true

date -u +"[phase1] finished_utc=%Y-%m-%dT%H:%M:%SZ"
echo "[phase1] result_dir=$RESULT_DIR"
