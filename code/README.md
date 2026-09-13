# Cancellation-path phenomenon audit

This directory is a **phenomenon-audit harness**, not a proposed scheduler.

## Local smoke test (no GPU)

Terminal 1:

```powershell
python mock_server.py --disconnect-policy honor --slots 2
```

Terminal 2:

```powershell
python cancel_probe.py --cancel-rate 0.2 --cancel-mode fin --output results/honor_fin.csv
python cancel_probe.py --cancel-rate 0.2 --cancel-mode rst --output results/honor_rst.csv
```

Repeat with `--disconnect-policy ignore`.  The mock comparison only verifies
that the probe can expose orphan work; it is **not evidence about vLLM/SGLang**.

## Real 5090 minimum experiment

1. Start one current vLLM release with a small open model (Qwen3-0.6B first,
   then 4B/8B if memory permits), streaming enabled and Prometheus metrics.
2. Run a no-cancellation control and FIN/RST cancellation sweeps.
3. Repeat through nginx with client-abort propagation enabled/disabled.
4. Poll runtime metrics (`num_requests_running`, `num_requests_waiting`, KV-cache
   usage, generated-token counters) at 50--100 ms if supported.
5. Repeat on current SGLang.  Pin exact commits and retain logs.

Core outputs must be: cancellation-to-slot-release latency, inferred post-cancel
tokens, survivor TTFT/TPOT/P99, and recovery time.  Client-side cancellation
count alone is insufficient.

## Kill conditions

- Current vLLM and SGLang stop within at most two decode iterations for every
  direct/proxy FIN/RST path.
- At 1--10% cancellation, survivor P95/P99 penalty is below 5% at realistic
  pre-saturation load.
- Proxy configuration alone removes the effect without a meaningful trade-off.
- A peer-reviewed work already evaluates the same cross-layer lifecycle path and
  server-side reclamation metrics.
