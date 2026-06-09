#!/usr/bin/env python3
"""Non-streaming tool-call probe: does mlx_lm.server parse Qwen tool template into structured tool_calls?"""
import json, time, urllib.request, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
p = json.load(open(os.path.join(HERE, "fixtures", "realistic_prompt_5k.json")))
body = {
    "model": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    "messages": [{"role": "system", "content": p["system"]},
                 {"role": "user", "content": "Compute 2 to the power of 20 by running code."}],
    "tools": p["tools"],
    "stream": False,
    "temperature": 0.1,
    "max_tokens": 256,
}
req = urllib.request.Request("http://127.0.0.1:%s/v1/chat/completions" % PORT,
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
t0 = time.monotonic()
with urllib.request.urlopen(req, timeout=1200) as r:
    o = json.load(r)
wall = time.monotonic() - t0
ch = o["choices"][0]
msg = ch.get("message", {})
print(f"wall={wall:.1f}s  finish_reason={ch.get('finish_reason')!r}")
print(f"prompt_tok={o.get('usage',{}).get('prompt_tokens','?')}  gen_tok={o.get('usage',{}).get('completion_tokens','?')}")
tcs = msg.get("tool_calls")
if tcs:
    fn = tcs[0]["function"]
    print(f"STRUCTURED tool_calls: PASS -> {fn['name']}({fn['arguments']})")
else:
    print(f"STRUCTURED tool_calls: NONE")
    print(f"content (raw): {(msg.get('content') or '')[:300]!r}")
