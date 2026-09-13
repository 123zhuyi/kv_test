#!/usr/bin/env python3
"""Merge cancellation-audit JSONL traces into request timelines and a kill-test report.

The analyzer never upgrades inferred evidence to observed evidence.  Missing
runtime hooks are reported as instrumentation gaps instead of being treated as
zero latency or zero post-cancel work.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


MILESTONES = {
    "CLIENT_REQUEST_START": "client_start_ns",
    "CLIENT_REQUEST_SENT": "client_sent_ns",
    "CLIENT_FIRST_TOKEN": "client_first_token_ns",
    "CLIENT_CANCEL_INIT": "client_cancel_ns",
    "CLIENT_CONNECTION_CLOSED": "client_closed_ns",
    "CLIENT_REQUEST_END": "client_end_ns",
    "SERVER_DISCONNECT_DETECTED": "server_disconnect_ns",
    "ABORT_EMITTED": "abort_emitted_ns",
    "SCHEDULER_ABORT_SEEN": "scheduler_abort_seen_ns",
    "REQUEST_LAST_SCHEDULED": "last_scheduled_ns",
    "KV_RELEASED": "kv_released_ns",
}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    rank = (len(xs) - 1) * q
    lo = math.floor(rank)
    hi = math.ceil(rank)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def ms_between(later: int | None, earlier: int | None) -> float | None:
    if later is None or earlier is None:
        return None
    return (later - earlier) / 1_000_000


def read_events(patterns: Iterable[str]) -> list[dict[str, Any]]:
    paths: list[str] = []
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        paths.extend(matches or ([pattern] if Path(pattern).exists() else []))
    events: list[dict[str, Any]] = []
    for name in sorted(set(paths)):
        with open(name, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{name}:{line_no}: invalid JSON: {exc}") from exc
                row["_file"] = name
                events.append(row)
    return events


def build_timelines(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        audit_id = event.get("audit_id")
        if audit_id:
            groups[str(audit_id)].append(event)

    rows: list[dict[str, Any]] = []
    for audit_id, evs in groups.items():
        evs.sort(key=lambda x: int(x.get("ts_monotonic_ns", 0)))
        row: dict[str, Any] = {
            "audit_id": audit_id,
            "runtime_request_id": next((e.get("runtime_request_id") for e in evs if e.get("runtime_request_id")), None),
            "runtime": next((e.get("runtime") for e in evs if e.get("runtime") and e.get("runtime") != "mock"), evs[0].get("runtime")),
        }
        start = next((e for e in evs if e.get("event") == "CLIENT_REQUEST_START"), None)
        extra = (start or {}).get("extra") or {}
        row.update(
            trial_index=extra.get("trial_index"),
            cancel_mode=extra.get("cancel_mode", "unknown"),
            cancel_after_events=extra.get("cancel_after_events"),
            model=extra.get("model"),
            max_tokens=extra.get("max_tokens"),
        )
        for event_name, column in MILESTONES.items():
            hits = [int(e["ts_monotonic_ns"]) for e in evs if e.get("event") == event_name and e.get("ts_monotonic_ns") is not None]
            row[column] = hits[0] if hits else None

        iterations = [e for e in evs if e.get("event") == "SCHEDULER_ITERATION"]
        cancel_ns = row.get("server_disconnect_ns") or row.get("client_cancel_ns")
        post = [e for e in iterations if cancel_ns is not None and int(e.get("ts_monotonic_ns", 0)) > cancel_ns]
        row["scheduler_iterations_total"] = len(iterations)
        row["post_cancel_scheduler_iterations"] = (
            len(post)
            if cancel_ns is not None and row.get("scheduler_abort_seen_ns") is not None
            else None
        )
        if row.get("last_scheduled_ns") is None and iterations:
            row["last_scheduled_ns"] = int(iterations[-1]["ts_monotonic_ns"])

        row["client_to_disconnect_ms"] = ms_between(row.get("server_disconnect_ns"), row.get("client_cancel_ns"))
        row["disconnect_to_abort_emit_ms"] = ms_between(row.get("abort_emitted_ns"), row.get("server_disconnect_ns"))
        row["abort_emit_to_scheduler_ms"] = ms_between(row.get("scheduler_abort_seen_ns"), row.get("abort_emitted_ns"))
        row["cancel_to_last_scheduled_ms"] = ms_between(row.get("last_scheduled_ns"), cancel_ns)
        row["cancel_to_kv_release_ms"] = ms_between(row.get("kv_released_ns"), cancel_ns)

        required = ["client_cancel_ns", "server_disconnect_ns", "abort_emitted_ns", "scheduler_abort_seen_ns", "kv_released_ns"]
        missing = [c.removesuffix("_ns") for c in required if row.get(c) is None]
        row["missing_observations"] = ";".join(missing)
        row["trace_complete"] = not missing
        row["source_files"] = ";".join(sorted({str(e.get("_file")) for e in evs}))
        rows.append(row)
    return sorted(rows, key=lambda r: (str(r.get("runtime")), str(r.get("cancel_mode")), r.get("cancel_after_events") or -1, r.get("trial_index") or -1))


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("runtime"), row.get("cancel_mode"), row.get("cancel_after_events"))].append(row)
    out: list[dict[str, Any]] = []
    for (runtime, mode, after), items in sorted(groups.items(), key=lambda x: str(x[0])):
        post = [float(r["post_cancel_scheduler_iterations"]) for r in items if r.get("post_cancel_scheduler_iterations") is not None]
        release = [float(r["cancel_to_kv_release_ms"]) for r in items if r.get("cancel_to_kv_release_ms") is not None]
        complete = sum(bool(r.get("trace_complete")) for r in items)
        out.append({
            "runtime": runtime,
            "cancel_mode": mode,
            "cancel_after_events": after,
            "trials": len(items),
            "complete_timelines": complete,
            "post_cancel_iterations_mean": sum(post) / len(post) if post else None,
            "post_cancel_iterations_p95": percentile(post, .95),
            "post_cancel_iterations_max": max(post) if post else None,
            "cancel_to_kv_release_ms_p50": percentile(release, .50),
            "cancel_to_kv_release_ms_p95": percentile(release, .95),
            "cancel_to_kv_release_ms_p99": percentile(release, .99),
        })
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["audit_id"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def render_report(summary: list[dict[str, Any]], timelines: list[dict[str, Any]]) -> str:
    required_cases = {(runtime, mode, n) for runtime in ("vllm", "sglang") for mode, ns in (("none", [None]), ("fin", [1, 8, 32]), ("rst", [8, 32])) for n in ns}
    observed = {(r.get("runtime"), r.get("cancel_mode"), r.get("cancel_after_events")) for r in timelines}
    five_trials = {(r["runtime"], r["cancel_mode"], r["cancel_after_events"]) for r in summary if int(r["trials"]) >= 5}
    missing_cases = sorted(required_cases - observed, key=str)
    incomplete_cases = sorted(required_cases - five_trials, key=str)
    cancel_rows = [r for r in timelines if r.get("cancel_mode") in ("fin", "rst")]
    complete = [r for r in cancel_rows if r.get("trace_complete")]
    post = [r.get("post_cancel_scheduler_iterations") for r in complete if r.get("post_cancel_scheduler_iterations") is not None]

    if missing_cases or incomplete_cases or len(complete) < len(cancel_rows):
        decision = "FIX_INSTRUMENTATION_OR_COMPLETE_TRIALS"
    elif post and max(post) <= 2:
        decision = "STOP/NO-GO"
    else:
        decision = "CONTINUE_NGINX"

    lines = [
        "# Phase-1 direct cancellation lifecycle summary",
        "",
        f"**Decision: {decision}**",
        "",
        "The decision uses only OBSERVED request-level hooks. Missing runtime events are not interpreted as zero work.",
        "",
        "| runtime | mode | after SSE | n | complete | post-cancel iter mean/p95/max | KV release p50/p95/p99 ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary:
        it = f"{r['post_cancel_iterations_mean']}/{r['post_cancel_iterations_p95']}/{r['post_cancel_iterations_max']}"
        kv = f"{r['cancel_to_kv_release_ms_p50']}/{r['cancel_to_kv_release_ms_p95']}/{r['cancel_to_kv_release_ms_p99']}"
        lines.append(f"| {r['runtime']} | {r['cancel_mode']} | {r['cancel_after_events']} | {r['trials']} | {r['complete_timelines']} | {it} | {kv} |")
    lines += ["", f"Missing cases: `{missing_cases}`", "", f"Cases with fewer than 5 trials: `{incomplete_cases}`", ""]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+", help="JSONL paths or glob patterns")
    p.add_argument("--output-dir", type=Path, default=Path("results/phase1"))
    args = p.parse_args()
    events = read_events(args.inputs)
    timelines = build_timelines(events)
    summary = summarize(timelines)
    write_csv(args.output_dir / "direct_request_timelines.csv", timelines)
    write_csv(args.output_dir / "direct_summary.csv", summary)
    (args.output_dir / "direct_summary.md").write_text(render_report(summary, timelines), encoding="utf-8")
    print(f"events={len(events)} requests={len(timelines)} groups={len(summary)}")


if __name__ == "__main__":
    main()
