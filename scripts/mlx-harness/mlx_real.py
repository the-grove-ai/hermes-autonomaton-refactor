#!/usr/bin/env python3
"""Measure MLX (mlx_lm.server, OpenAI endpoint) at the LARGE prefill fixture (~15K-token system + 22 tools).

WARNING: this drives the large prefill that OOM'd the 24 GB M5 in Sprint 71. Do NOT run unguarded —
start mlx_watchdog.py alongside it. See README.
"""
import json, time, urllib.request, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
p = json.load(open(os.path.join(HERE, "fixtures", "realistic_prompt_synth_15k.json")))
SYSTEM, TOOLS = p["system"], p["tools"]
print(f"MLX realistic test: ~{len(SYSTEM)//4}tok system + {len(TOOLS)} tools  (port {PORT})", flush=True)

def run(user):
    body = {
        "model": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "tools": TOOLS,
        "stream": True,
        "temperature": 0.1,
        "max_tokens": 256,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request("http://127.0.0.1:%s/v1/chat/completions" % PORT,
                                data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    t0 = time.monotonic(); tf = None; n = 0; txt = ""; tcs = None; usage = None
    with urllib.request.urlopen(req, timeout=1200) as r:
        for ln in r:
            ln = ln.strip()
            if not ln or not ln.startswith(b"data:"):
                continue
            data = ln[5:].strip()
            if data == b"[DONE]":
                break
            o = json.loads(data)
            if o.get("usage"):
                usage = o["usage"]
            ch = (o.get("choices") or [{}])[0]
            delta = ch.get("delta", {})
            if delta.get("content"):
                if tf is None: tf = time.monotonic()
                txt += delta["content"]; n += 1
            if delta.get("tool_calls"):
                if tf is None: tf = time.monotonic()
                tcs = delta["tool_calls"]
    return t0, tf, time.monotonic(), n, txt, tcs, usage

for label, user in [("COLD large-prefill: tool-trigger", "Compute 2 to the power of 20 by running code."),
                    ("WARM: reasoning", "What's 17 times 23? One line.")]:
    t0, tf, te, n, txt, tcs, usage = run(user)
    ttft = (tf - t0) if tf else -1
    gen = (usage or {}).get("completion_tokens", n)
    dtps = gen / (te - tf) if tf and te > tf else 0
    print("-" * 58, flush=True)
    print(f"[{label}]", flush=True)
    print(f"  prompt_tok={(usage or {}).get('prompt_tokens','?')}  gen_tok={gen}  TTFT={ttft:.1f}s  decode={dtps:.1f}t/s  wall={te-t0:.1f}s", flush=True)
    print(f"  TOOL: {tcs[0]['function']['name'] if tcs else 'none'}   TXT: {txt.strip()[:120]!r}", flush=True)
