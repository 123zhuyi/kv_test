#!/usr/bin/env python3
"""Tiny continuous-batching-like SSE server for validating cancel_probe.py.

It is not evidence about a real LLM runtime.  `--disconnect-policy ignore`
intentionally models an orphan request that keeps a scarce execution slot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter


class State:
    def __init__(self, slots: int, policy: str, token_ms: float):
        self.sem = asyncio.Semaphore(slots)
        self.policy = policy
        self.token_s = token_ms / 1000
        self.c = Counter()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: State):
    state.c["connections_total"] += 1
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        first = header.split(b"\r\n", 1)[0].decode("latin1")
        path = first.split()[1]
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        body = await reader.readexactly(length) if length else b""
        if path == "/metrics":
            payload = "\n".join(f"cancel_audit_{k} {v}" for k, v in state.c.items()) + "\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload.encode()
            )
            await writer.drain()
            return
        req = json.loads(body or b"{}")
        max_tokens = int(req.get("max_tokens", 128))
        state.c["waiting"] += 1
        async with state.sem:
            state.c["waiting"] -= 1
            state.c["running"] += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            disconnected = False
            for i in range(max_tokens):
                await asyncio.sleep(state.token_s)
                state.c["tokens_generated_total"] += 1
                if not disconnected:
                    payload = json.dumps({"choices": [{"delta": {"content": str(i)}}]})
                    try:
                        writer.write(f"data: {payload}\n\n".encode())
                        await writer.drain()
                    except (ConnectionError, OSError):
                        disconnected = True
                        state.c["disconnects_detected_total"] += 1
                        if state.policy == "honor":
                            state.c["requests_aborted_total"] += 1
                            break
            if not disconnected:
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
            state.c["running"] -= 1
            state.c["requests_finished_total"] += 1
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        state.c["early_disconnects_total"] += 1
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def main(args):
    state = State(args.slots, args.disconnect_policy, args.token_ms)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, state), args.host, args.port
    )
    print(
        f"mock server on http://{args.host}:{args.port}, "
        f"policy={args.disconnect_policy}, slots={args.slots}, token_ms={args.token_ms}"
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18000)
    p.add_argument("--slots", type=int, default=2)
    p.add_argument("--token-ms", type=float, default=20)
    p.add_argument("--disconnect-policy", choices=["honor", "ignore"], default="honor")
    asyncio.run(main(p.parse_args()))
