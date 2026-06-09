# mlx-harness — local-inference measurement harness (Sprint 71 → 77 arc)

Instrumentation for the local T2 substrate: **Qwen3-Coder-30B-A3B on `mlx_lm.server`**, M5 Mac (24 GB
unified memory — `hw.memsize` = 24 GiB exactly). These are operator-run measurement scripts, committed
so the prefill knee can be measured, the bake-off can rerun, and the tool-call XML grammar can be captured
in a later sprint. Sprint 71 built them and left them uncommitted; Sprint 77.0 lands them in-tree.

All scripts are standalone (stdlib only) and assume the inference server runs **locally on the M5** —
they target `127.0.0.1`, not the Tailscale IP. The Tailscale binding (VM → Mac) is the binding sprint's
concern, not the harness's.

## Scripts

| Script | What it does | Server | Port |
|--------|--------------|--------|------|
| `mlx_probe_nostream.py` | **Tool-call grammar probe.** Non-streaming request; reports whether `message.tool_calls` came back structured (PASS) or whether the call is sitting raw in `message.content`. This is the tool that captures the literal XML emission for the XML→`tool_calls` parser. | `mlx_lm.server` | 8080 |
| `mlx_real_5k.py` | Streaming measure at the **~5K** prefill (the Sprint 71 *survived* point): prompt tokens, TTFT, decode t/s, tool emission. | `mlx_lm.server` | 8080 |
| `mlx_real.py` | Streaming measure at the **large** prefill (synthetic ~15K-token system + 22 tools). **The box-crasher — see warning below.** | `mlx_lm.server` | 8080 |
| `mlx_watchdog.py` | **Andon OOM watchdog.** Samples available RAM via `vm_stat`; on 3 consecutive breaches of the floor, `SIGKILL`s `mlx_lm.server` to prevent a hard OOM crash. Fail loud. | local | — |
| `bake_measure.py` | T2 bake-off, native Ollama `/api/chat`, synthetic weather tool + repo `affordances.md` as system. | Ollama | 11434 |
| `bake_real.py` | Bake-off replaying the **real** newest gateway session. Operator-run only — see note. | Ollama | 11434 |

**Port:** every `mlx_*` script defaults to **8080** (mlx_lm.server's default) and takes an override as
`argv[1]`, e.g. `python mlx_real_5k.py 8080`. (8081 was the Sprint 71 *llama-server/gemma* profile, a
different substrate; the Ollama bake scripts use 11434.)

## ⚠️ Do NOT run `mlx_real.py` unguarded

`mlx_real.py` drives the large prefill that **OOM'd the 24 GB M5 in Sprint 71** (a ~24K prefill exceeded
memory and crashed the box; a ~5K prefill survived; the true cliff between is unmeasured). Always start the
watchdog in another terminal first:

```bash
python mlx_watchdog.py          # terminal 1 — Andon guard
python mlx_real.py 8080         # terminal 2 — the large-prefill run
```

Measuring that knee under the guard is the binding sprint's (77.1) job, not this one.

### Watchdog cadence — a 77.1 validation item, not yet trusted

`mlx_watchdog.py` uses `FLOOR_GB = 1.0` with a 3-strike rule at a 2 s sampling interval — up to ~6 s below
the floor before it kills the server. A prefill activation spike can be **sub-second**, so the watchdog may
not sample fast enough to pre-empt a kernel OOM-kill on the steep part of the curve. **The guard must be
proven against a real spike before it is trusted.** Treat the current cadence as a starting point to
validate (and likely tighten) in 77.1, not a settled safety mechanism.

## Fixtures (`fixtures/`)

- `realistic_prompt_5k.json` — the real ~5K prefill fixture (system ~2,500 tok + 7 tools). The system text
  is the public constitution scaffold; verified free of secrets/PII.
- `realistic_prompt_synth_15k.json` — a **synthetic** large fixture (~15K-token generic system filler + 22
  synthetic tool schemas), structurally matching the real large prefill but carrying **zero real operator
  content**. Generated content only; substituted for the operator-PII original, which is intentionally not
  committed.

`bake_real.py` reads its input live from `~/.grove/sessions/session_*.json` (real operator system prompt +
tools) — that input is **not** committed and the script is **not portable or smoke-tested**: it requires a
populated `~/.grove/sessions` on this machine, and its captured output must never be committed (real
operator content).

## Smoke test (no server required)

The committed safe-subset check: `py_compile` all six scripts, plus `mlx_watchdog.avail_gb()` returns a
sane positive number. The server-driven probes (`mlx_real*`, `mlx_probe_nostream`, `bake_*`) require a
running inference server and are **not** part of the smoke test — `mlx_real.py` in particular is never
executed at commit time.
