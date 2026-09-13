# Cancellation Audit — Data Freeze Manifest (2026-09-13)

This repository freezes the complete evidence base for the LLM serving
cancellation-path audit, including the decisive KV-bound broken-vs-fixed A/B
result (survivor TTFT p95: **6818 ms broken vs 59.8 ms fixed** at 40% client
cancellation, Qwen3-8B / RTX 5090).

## 1. Hardware / Platform

- GPU: NVIDIA GeForce RTX 5090, 32607 MiB, single card (AutoDL/SeetaCloud rented instance)
- Driver: 580.76.05, CUDA 13.0
- Host: Linux container (`autodl-container-a03b4fb96c`), x86-64
- Full `nvidia-smi`, `uname`, `pip freeze`: `freeze_2026-09-13/code_state.tar.gz` → `env/`

## 2. Runtime Versions

| component | version |
|---|---|
| sglang | 0.5.19 (latest release as of 2026-09-13; contains the cancellation bug) |
| torch | 2.13.0+cu130 |
| sglang-kernel | 0.4.6.post1 |
| triton | 3.7.1 |
| flashinfer-python | 0.6.18 (installed; not used — incompatible with sm120, see launch flags) |
| Python | 3.12 (venv at `/root/autodl-tmp/cancel-audit/.venv` on the instance) |

## 3. Models

| model | source | files | config.json sha256 |
|---|---|---|---|
| Qwen3-0.6B | modelscope `Qwen/Qwen3-0.6B` | see `env/model_Qwen3-0.6B_files.txt` | in same file |
| Qwen3-8B | modelscope `Qwen/Qwen3-8B` (~16 GB, bf16) | see `env/model_Qwen3-8B_files.txt` | `f7c4eadfbbf522470667b797a3c89be2524832d2d599797248dc304fff447c30` |

## 4. Server Launch Flags (all SGLang runs)

```bash
python -m sglang.launch_server \
  --model-path <MODEL_DIR> --host 127.0.0.1 --port 18000 \
  --trust-remote-code \
  --attention-backend triton \
  --sampling-backend pytorch \
  --disable-cuda-graph \
  --mem-fraction-static 0.60
```

Notes: FlashInfer default backend fails on RTX 5090 (sm120); the venv's CUDA-13
nvcc must be on PATH (see `code/remote_sglang_capacity.sh`). With 8B bf16 at
`--mem-fraction-static 0.60`, the server reports `max_total_num_tokens=22720`
(KV-limited concurrency ≈ 41 requests @ ~550 tokens each) — this is the
KV-pressure condition of the decisive A/B.

## 5. Broken / Fixed Code Construction

Both arms are the same 0.5.19 wheel with trace instrumentation; only the
cancellation-path code differs:

- **pristine**: `code_state/tokenizer_manager.py.PRISTINE_0519`, `scheduler.py.PRISTINE_0519`, `common.py.PRISTINE_0519`
- **broken arm** = pristine + `code/instrument_sglang_0519.py` hooks (live copy: `*.live_broken_backup`)
- **fixed arm** = pristine + PR [#35255](https://github.com/sgl-project/sglang/pull/35255) backport (`code_state/pr35255.diff`; 10/12 hunks applied cleanly by `patch`, `_mark_state_dispatched` def hand-applied by `code/instrument_sglang_fixed.py`, one hunk already present) + fixed-code trace hooks. Patched-but-uninstrumented reference: `code_state/prtest_patched/` and `*.PRISTINE_plus_PR35255`.
- Upstream main at audit time: `code_state/tm_main.py` (contains the official fix).
- Per-run instrumentation diffs: each results tarball contains `provenance/*.diff`.

## 6. Workload / Harness Parameters

Client: `code/cancel_probe.py` — raw-socket OpenAI-compatible SSE client, TCP FIN
(`writer.close()`) after 8 received SSE events; `ignore_eos=True`; identical
prompt for all requests ("Write an unbroken numbered list from 1 upward...");
radix cache makes prefill ~free; decode-dominated workload.

Common: `generation_seed=20260913`, `max_tokens=512` (capacity/8B runs), Poisson
open-loop arrivals.

| experiment | dir in tarball | rates (req/s) | cancel rates | requests/case | probe seeds |
|---|---|---|---|---|---|
| 0.6B capacity run 1 | `capacity_remote` | 1/2/4 | 0/10/20/40% | 80 (calib 60) | calib 4001-4003, main 5001-5004 |
| 0.6B capacity run 2 | `capacity_remote2` | 4/8/16/32 | 0/10/20/40% | 100 (calib 60) | same scheme |
| 8B round 1 (unsaturated) | `capacity8b_remote` | 0.5/1/2/4 | 0/10/20/40% | 80 (calib 60) | same scheme |
| 8B round 2, **broken arm @ KV saturation** | `capacity8b2_remote` | 2/4/8/12 | 0/10/20/40% @ r=12 | 100 (calib 60) | same scheme |
| 8B **fixed arm @ KV saturation** | `fixed8b_remote` | 12 | 0/10/20/40% | 100 (calib 60) | same scheme |
| mechanism verification (0.6B) | `mechanism_remote` | 2 | 50% | 16 | 777 |
| phase-1 direct (0.6B) | `phase1_remote` | serial | 100% | per matrix | see `run_phase1_direct.py` |
| mixed stress (0.6B) | `stress_remote` | 0.5/1.0 | 0/5/10/20% | 40 | 1001-1008 |
| blocker interference (0.6B) | `interference_remote` | burst+5 | per case | 1/4/8 blockers + 20 survivors | 2000+/3000+ |

## 7. File Inventory

- `code/` — all experiment scripts (client, matrices, remote runners, instrumentation, analysis).
- `docs/CANCELLATION_AUDIT_DECISION_2026-09-13.md` — decision document with the full A/B table and root-cause chain.
- `freeze_2026-09-13/*.tar.gz` — per-experiment raw data: probe CSVs, client JSONL traces, `runtime_trace.jsonl` (OBSERVED runtime events), `server.log`, summaries, manifests, instrumentation provenance diffs.
- `freeze_2026-09-13/code_state.tar.gz` — everything in §5 plus `env/` (pip freeze, nvidia-smi, model manifests).
- `freeze_2026-09-13/SHA256SUMS.txt` — integrity checksums (paths are remote-absolute; compare by basename).

## 8. Reproduction

1. RTX 5090 (or sm120 GPU), install sglang==0.5.19 with torch 2.13.0+cu130; venv CUDA-13 nvcc on PATH.
2. Download Qwen3-0.6B / Qwen3-8B (modelscope).
3. Broken arm: `python code/instrument_sglang_0519.py <site-packages>/sglang`.
   Fixed arm: apply `code_state/pr35255.diff` to the pristine files (hand-add `_mark_state_dispatched`), then `python code/instrument_sglang_fixed.py <site-packages>/sglang`.
4. Launch server with §4 flags; run e.g.
   `bash code/remote_sglang_capacity.sh` with `MODEL_DIR`, `CALIB_RATES`, `MAIN_REQUESTS` env vars set per §6.
5. Analysis: `code/analyze_lifecycle.py` merges `*.client.jsonl` + `runtime_trace.jsonl`; per-case metrics are recomputed from probe CSVs by `code/run_capacity_loss.py`.
