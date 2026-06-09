#!/usr/bin/env python3
"""Replay the REAL operator system prompt + tools (from the newest live gateway session) through a model.

OPERATOR-RUN INSTRUMENT. Reads live ~/.grove/sessions/session_*.json (real operator system prompt + tools).
NOT portable and NOT smoke-tested: requires a populated ~/.grove/sessions on this machine, and its input
carries real operator content — never commit captured output. See README.
"""
import json, sys, time, urllib.request, glob, os

MODEL = sys.argv[1]
HOST = "http://127.0.0.1:11434"

# newest real gateway session
sess = max(glob.glob(os.path.expanduser("~/.grove/sessions/session_*.json")), key=os.path.getmtime)
d = json.load(open(sess))
SYSTEM = d["system_prompt"]
TOOLS = d["tools"]
print(f"session   : {os.path.basename(sess)}")
print(f"system    : {len(SYSTEM)} chars (~{len(SYSTEM)//4} tok)   tools: {len(TOOLS)}")
print(f"model     : {MODEL}\n")

USER_TURNS = [
    "My name is Jim and I'm testing turn stability. Acknowledge in one line.",
    "What's 17 times 23? One line.",
    "Compute 2 to the power of 20 by running code.",   # should trigger execute_code
    "I need help planning a 3-step research task. Briefly, what would you do?",
]

def run(user, warm=False):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "tools": TOOLS,
        "think": False,
        "stream": True,
        "options": {"num_ctx": 24576},
    }
    req = urllib.request.Request(HOST + "/api/chat", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    t0 = time.monotonic(); t_first = None; final = None; tcs = None; text = ""
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.strip()
            if not line: continue
            o = json.loads(line); m = o.get("message", {})
            if t_first is None and (m.get("content") or m.get("tool_calls")):
                t_first = time.monotonic()
            if m.get("content"): text += m["content"]
            if m.get("tool_calls"): tcs = m["tool_calls"]
            if o.get("done"): final = o
    t_end = time.monotonic()
    return t0, t_first, t_end, final, tcs, text

# warm once (cache the big system prefix)
print("warming (loading + caching system prefix)...", flush=True)
run(USER_TURNS[0], warm=True)

for u in USER_TURNS:
    t0, t_first, t_end, f, tcs, text = run(u)
    ttft = (t_first - t0) if t_first else 0
    pe_n, pe_d = f.get("prompt_eval_count"), f.get("prompt_eval_duration", 0)/1e9
    ev_n, ev_d = f.get("eval_count"), f.get("eval_duration", 0)/1e9
    dtps = (ev_n/ev_d) if ev_n and ev_d else 0
    print("-"*60)
    print(f"USER: {u}")
    print(f"  prompt_tok={pe_n}  gen_tok={ev_n}  TTFT={ttft:.2f}s  decode={dtps:.1f}t/s  wall={t_end-t0:.2f}s")
    if tcs:
        print(f"  TOOL_CALL: {tcs[0]['function']['name']}({json.dumps(tcs[0]['function'].get('arguments'))})")
    if text.strip():
        print(f"  TEXT: {text.strip()[:220]!r}")
