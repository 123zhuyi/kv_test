#!/usr/bin/env python3
"""OpenAI-compatible SSE cancellation probe using only the Python stdlib.

This is deliberately a transport probe, not an LLM benchmark.  It can close a
request with a TCP FIN or RST after the complete HTTP body has been sent and
records a per-request timeline.  The same client can target a real vLLM/SGLang
endpoint or the bundled mock_server.py.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import socket
import struct
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass
class Result:
    audit_id: str
    request_id: str
    cancelled: bool
    cancel_mode: str
    scheduled_s: float
    started_s: float
    sent_s: float
    first_event_s: float | None
    cancel_s: float | None
    ended_s: float
    sse_events: int
    bytes_received: int
    completed: bool
    error: str | None


def now() -> float:
    return time.perf_counter()


def wall_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class TraceWriter:
    """Append client lifecycle evidence as JSONL (one probe process per file)."""

    def __init__(self, path: Path, runtime: str) -> None:
        self.path = path
        self.runtime = runtime
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8", buffering=1)

    def emit(
        self,
        *,
        audit_id: str,
        request_id: str,
        event: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "ts_monotonic_ns": time.monotonic_ns(),
            "ts_wall": wall_now(),
            "runtime": self.runtime,
            "source": "client",
            "evidence": "OBSERVED",
            "audit_id": audit_id,
            "runtime_request_id": request_id,
            "event": event,
            "iteration": None,
            "running_count": None,
            "extra": extra or {},
        }
        self._file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._file.close()


async def close_stream(writer: asyncio.StreamWriter, mode: str) -> None:
    if mode == "rst":
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.transport.abort()
        return
    writer.close()  # orderly FIN
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def read_http_stream(
    reader: asyncio.StreamReader,
    *,
    cancel_at: float | None,
    cancel_after_events: int | None,
    writer: asyncio.StreamWriter,
    cancel_mode: str,
    trace: TraceWriter,
    audit_id: str,
    request_id: str,
) -> tuple[int, int, float | None, float | None, bool]:
    buf = bytearray()
    events = 0
    bytes_received = 0
    first_event = None
    cancel_time = None
    completed = False
    headers_done = False

    while True:
        timeout = None if cancel_at is None else max(0.0, cancel_at - now())
        if timeout == 0.0:
            cancel_time = now()
            trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CANCEL_INIT", extra={"mode": cancel_mode, "trigger": "delay", "sse_events": events})
            await close_stream(writer, cancel_mode)
            trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CONNECTION_CLOSED", extra={"mode": cancel_mode})
            break
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        except asyncio.TimeoutError:
            cancel_time = now()
            trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CANCEL_INIT", extra={"mode": cancel_mode, "trigger": "delay", "sse_events": events})
            await close_stream(writer, cancel_mode)
            trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CONNECTION_CLOSED", extra={"mode": cancel_mode})
            break
        if not chunk:
            break
        bytes_received += len(chunk)
        buf.extend(chunk)
        if not headers_done:
            split = buf.find(b"\r\n\r\n")
            if split < 0:
                continue
            status = bytes(buf[:split]).split(b"\r\n", 1)[0]
            if b" 2" not in status:
                raise RuntimeError(status.decode("latin1", "replace"))
            del buf[: split + 4]
            headers_done = True

        # Counting SSE data records is sufficient for cancellation accounting;
        # chunk framing is harmless because records still contain "data:" lines.
        while True:
            marker = buf.find(b"data:")
            if marker < 0:
                if len(buf) > 16:
                    del buf[:-16]
                break
            end = buf.find(b"\n\n", marker)
            if end < 0:
                break
            payload = bytes(buf[marker + 5 : end]).strip()
            del buf[: end + 2]
            events += 1
            if first_event is None:
                first_event = now()
                trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_FIRST_TOKEN", extra={"sse_event": events})
            if payload == b"[DONE]":
                completed = True
            if cancel_after_events is not None and events >= cancel_after_events and not completed:
                cancel_time = now()
                trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CANCEL_INIT", extra={"mode": cancel_mode, "trigger": "sse_events", "sse_events": events})
                await close_stream(writer, cancel_mode)
                trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_CONNECTION_CLOSED", extra={"mode": cancel_mode})
                return events, bytes_received, first_event, cancel_time, completed
    return events, bytes_received, first_event, cancel_time, completed


async def issue_one(args: argparse.Namespace, index: int, scheduled: float, doomed: bool, trace: TraceWriter) -> Result:
    await asyncio.sleep(max(0.0, scheduled - now()))
    started = now()
    # Keep the audit and runtime IDs identical so deep scheduler hooks can
    # correlate a request without propagating an extra HTTP header internally.
    audit_id = f"cancel-audit-{args.run_id}-{index:06d}-{uuid.uuid4().hex[:8]}"
    request_id = audit_id
    trace.emit(
        audit_id=audit_id,
        request_id=request_id,
        event="CLIENT_REQUEST_START",
        extra={
            "trial_index": index,
            "selected_for_cancel": doomed,
            "cancel_mode": args.cancel_mode if doomed else "none",
            "cancel_after_events": args.cancel_after_events if doomed else None,
            "cancel_delay_s": args.cancel_delay if doomed else None,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "generation_seed": args.generation_seed,
        },
    )
    parts = urlsplit(args.url)
    if parts.scheme != "http":
        raise ValueError("Phase-1 FIN/RST probe currently supports direct HTTP only")
    port = parts.port or 80
    path = parts.path or "/v1/chat/completions"
    if parts.query:
        path += "?" + parts.query

    payload = {
            "model": args.model,
            "request_id": request_id,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens,
            "stream": True,
            "ignore_eos": True,
            "seed": args.generation_seed,
        }
    if args.runtime == "sglang":
        payload["rid"] = request_id
    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode()
    headers = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {parts.hostname}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"X-Request-Id: {request_id}\r\n"
        f"X-Cancel-Audit-ID: {audit_id}\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n\r\n"
    ).encode()

    sent = started
    first = cancel_t = None
    events = byte_count = 0
    completed = False
    error = None
    writer = None
    try:
        reader, writer = await asyncio.open_connection(parts.hostname, port)
        writer.write(headers + body)
        await writer.drain()
        sent = now()
        trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_REQUEST_SENT", extra={"bytes": len(headers) + len(body)})
        cancel_at = (
            sent + args.cancel_delay
            if doomed and args.cancel_after_events is None
            else None
        )
        events, byte_count, first, cancel_t, completed = await read_http_stream(
            reader,
            cancel_at=cancel_at,
            cancel_after_events=args.cancel_after_events if doomed else None,
            writer=writer,
            cancel_mode=args.cancel_mode,
            trace=trace,
            audit_id=audit_id,
            request_id=request_id,
        )
    except Exception as exc:  # recorded, not allowed to kill the whole sweep
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if writer is not None and not writer.is_closing():
            await close_stream(writer, "fin")
    ended = now()
    trace.emit(audit_id=audit_id, request_id=request_id, event="CLIENT_REQUEST_END", extra={"completed": completed, "error": error, "sse_events": events})
    return Result(
        audit_id=audit_id,
        request_id=request_id,
        cancelled=doomed,
        cancel_mode=args.cancel_mode if doomed else "none",
        scheduled_s=scheduled,
        started_s=started,
        sent_s=sent,
        first_event_s=first,
        cancel_s=cancel_t,
        ended_s=ended,
        sse_events=events,
        bytes_received=byte_count,
        completed=completed,
        error=error,
    )


async def run(args: argparse.Namespace) -> list[Result]:
    rng = random.Random(args.seed)
    t0 = now() + 0.2
    trace = TraceWriter(args.trace_output, args.runtime)
    try:
        tasks = []
        for i in range(args.requests):
            # Exponential inter-arrivals give an open-loop Poisson stream.
            offset = 0.0 if i == 0 else rng.expovariate(args.request_rate)
            t0 += offset
            doomed = rng.random() < args.cancel_rate
            tasks.append(asyncio.create_task(issue_one(args, i, t0, doomed, trace)))
        return await asyncio.gather(*tasks)
    finally:
        trace.close()


def export(results: list[Result], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(r) for r in results)
    survivors = [r for r in results if not r.cancelled and r.first_event_s is not None]
    ttft = sorted(1000 * (r.first_event_s - r.sent_s) for r in survivors)
    e2e = sorted(1000 * (r.ended_s - r.sent_s) for r in survivors)

    def percentile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        rank = (len(values) - 1) * q
        lo = int(rank)
        hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (rank - lo)

    summary = {
        "requests": len(results),
        "cancellation_selected": sum(r.cancelled for r in results),
        "cancellation_observed": sum(r.cancel_s is not None for r in results),
        "errors": sum(r.error is not None for r in results),
        "completed": sum(r.completed for r in results),
        "survivor_mean_ttft_ms": sum(ttft) / len(ttft) if ttft else None,
        "survivor_p95_ttft_ms": percentile(ttft, 0.95),
        "survivor_mean_e2e_ms": sum(e2e) / len(e2e) if e2e else None,
        "survivor_p95_e2e_ms": percentile(e2e, 0.95),
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18000/v1/chat/completions")
    p.add_argument("--model", default="mock-model")
    p.add_argument("--prompt", default="Count upward, one number per line.")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--requests", type=int, default=40)
    p.add_argument("--request-rate", type=float, default=5.0)
    p.add_argument("--cancel-rate", type=float, default=0.2, help="fraction in [0,1]")
    p.add_argument("--cancel-delay", type=float, default=0.4)
    p.add_argument(
        "--cancel-after-events",
        type=int,
        default=None,
        help="cancel after N received SSE events (decode-phase targeting); overrides delay",
    )
    p.add_argument("--cancel-mode", choices=["fin", "rst"], default="fin")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--generation-seed", type=int, default=1)
    p.add_argument("--runtime", choices=["mock", "vllm", "sglang"], default="mock")
    p.add_argument("--output", type=Path, default=Path("results/probe.csv"))
    p.add_argument("--trace-output", type=Path, default=None)
    args = p.parse_args()
    if not (0 <= args.cancel_rate <= 1):
        p.error("--cancel-rate must be in [0,1]")
    if args.request_rate <= 0:
        p.error("--request-rate must be > 0")
    if args.cancel_after_events is not None and args.cancel_after_events < 1:
        p.error("--cancel-after-events must be >= 1")
    args.run_id = uuid.uuid4().hex[:12]
    if args.trace_output is None:
        args.trace_output = args.output.with_suffix(".client.jsonl")
    return args


if __name__ == "__main__":
    ns = parse_args()
    rows = asyncio.run(run(ns))
    export(rows, ns.output)
