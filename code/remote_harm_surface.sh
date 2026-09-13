#!/usr/bin/env bash
# Harm-surface matrix driver: broken/fixed arms x request-rate levels x reps.
# Reuses the existing per-arm runners (they re-instrument from their own
# .orig backups, so arm switching is safe). Each cell gets its own RESULT_DIR.
set -uo pipefail

ROOT="${ROOT:-/root/autodl-tmp/cancel-audit}"
MODEL_DIR="${MODEL_DIR:-/root/autodl-tmp/models/Qwen3-8B}"
cd "$ROOT"

# cell list: "arm_tag:runner_script:rate:rep"
CELLS=(
  "broken:remote_sglang_capacity.sh:4:1"
  "broken:remote_sglang_capacity.sh:8:1"
  "broken:remote_sglang_capacity.sh:12:2"
  "broken:remote_sglang_capacity.sh:12:3"
  "broken:remote_sglang_capacity.sh:16:1"
  "fixed:remote_sglang_fixed_ab.sh:4:1"
  "fixed:remote_sglang_fixed_ab.sh:8:1"
  "fixed:remote_sglang_fixed_ab.sh:12:2"
  "fixed:remote_sglang_fixed_ab.sh:12:3"
  "fixed:remote_sglang_fixed_ab.sh:16:1"
)

echo "[surface] started $(date -u +%Y-%m-%dT%H:%M:%SZ) cells=${#CELLS[@]}"

wait_port_free() {
  for _ in $(seq 1 60); do
    if ! ss -ltn 2>/dev/null | grep -q ':18000 '; then return 0; fi
    sleep 5
  done
  echo "[surface] WARN: port 18000 still busy after 300s"
}

for cell in "${CELLS[@]}"; do
  IFS=':' read -r tag script rate rep <<< "$cell"
  rd="$ROOT/results/surface8b/${tag}_r${rate}_rep${rep}"
  mkdir -p "$rd"
  echo "[surface] === CELL $tag r=$rate rep=$rep $(date -u +%H:%M:%SZ) -> $rd"
  wait_port_free
  MODEL_DIR="$MODEL_DIR" \
  RESULT_DIR="$rd/sglang" \
  CALIB_RATES="$rate" \
  MAIN_REQUESTS=100 \
  CALIB_REQUESTS=60 \
    bash "$ROOT/$script" > "$rd/runner.log" 2>&1
  rc=$?
  echo "[surface] === CELL DONE $tag r=$rate rep=$rep rc=$rc $(date -u +%H:%M:%SZ)"
  sleep 10
done

echo "[surface] finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
