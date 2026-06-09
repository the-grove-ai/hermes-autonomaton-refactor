#!/usr/bin/env python3
"""Apples-to-apples T2 bake-off harness. Native Ollama /api/chat, precise timing."""
import json, sys, time, urllib.request, os

MODEL = sys.argv[1]
HOST = "http://127.0.0.1:11434"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
SYSTEM = open(os.path.join(REPO, "config", "identity", "affordances.md")).read()

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City and state, e.g. Indianapolis, IN"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["location"],
        },
    },
}]

def call(stream=True):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "What's the weather?"},
        ],
        "tools": TOOLS,
        "think": False,
        "stream": stream,
        "options": {"num_ctx": 24576},
    }
    req = urllib.request.Request(
        HOST + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    t_first = None
    final = None
    tool_calls = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get("message", {})
            if t_first is None and (msg.get("content") or msg.get("tool_calls")):
                t_first = time.monotonic()
            if msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
            if obj.get("done"):
                final = obj
    t_end = time.monotonic()
    return t0, t_first, t_end, final, tool_calls

# Warmup (load model into GPU; ignore timing)
print(f"[{MODEL}] warmup...", flush=True)
call()

# Measured run
print(f"[{MODEL}] measured run...", flush=True)
t0, t_first, t_end, final, tool_calls = call()

ttft = (t_first - t0) if t_first else None
wall = t_end - t0
pe_n = final.get("prompt_eval_count")
pe_d = final.get("prompt_eval_duration", 0) / 1e9
ev_n = final.get("eval_count")
ev_d = final.get("eval_duration", 0) / 1e9
decode_tps = (ev_n / ev_d) if ev_n and ev_d else None
prefill_tps = (pe_n / pe_d) if pe_n and pe_d else None

print("=" * 52)
print(f"MODEL              : {MODEL}")
print(f"prompt tokens      : {pe_n}  (prefill {prefill_tps:.1f} t/s)" if prefill_tps else f"prompt tokens: {pe_n}")
print(f"generated tokens   : {ev_n}")
print(f"TTFT               : {ttft:.2f} s" if ttft else "TTFT: n/a")
print(f"DECODE             : {decode_tps:.2f} t/s" if decode_tps else "decode: n/a")
print(f"total wall-clock   : {wall:.2f} s")
print(f"tool_call emitted  : {'YES -> ' + tool_calls[0]['function']['name'] if tool_calls else 'NO'}")
if tool_calls:
    print(f"  args             : {json.dumps(tool_calls[0]['function'].get('arguments'))}")
print("=" * 52)
