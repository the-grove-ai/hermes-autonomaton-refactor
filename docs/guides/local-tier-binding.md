# Local Tier Binding — running Tier 2 on-device with Ollama

The Cognitive Router splits work across four tiers of cognition. By default
all four are bound to cloud models. This guide moves Tier 2 — the daily
driver, where most interactions land — onto a model running on your own
machine.

Nothing about the router changes. A tier binding is a line of declarative
config; this guide edits two of those lines and restarts. The result is a
daily driver that costs nothing per interaction and keeps routine work on
hardware you control.

## What this does

Tier 2 is the default tier. Most requests — drafting, analysis, code,
conversation — run there. Bind it to a local model and that traffic stops
metering against a cloud API. Classification stays cheap on the cloud,
the hard problems still escalate to a cloud apex model, and everything in
between runs on-device.

The cost shape, for a representative day of 100 interactions:

| Turn type            | Tier             | Daily cost |
|----------------------|------------------|------------|
| Classification       | T1 Haiku (cloud) | ~$0.20     |
| Routine work         | T2 Gemma 4 (local) | $0       |
| Hard problems (~5%)  | T3 Opus (cloud)  | ~$0.40     |
| **Total**            |                  | **~$0.60** |

The same workload runs roughly $2.00/day on an all-cloud mid-tier model
and roughly $8.00/day on an all-cloud apex model. The local binding is
the difference.

## Before you start

- A machine with enough memory to hold the model. A 9–10 GB model needs
  roughly 16 GB of system or video memory free while it runs.
- The autonomaton already installed and run once, so that
  `~/.grove/routing.config.yaml` exists.

## Step 1 — Install Ollama

Ollama serves local models over an OpenAI-compatible HTTP endpoint. The
autonomaton talks to that endpoint directly — no proxy, no adapter.

On macOS, install the app from <https://ollama.com/download>, or use
Homebrew:

```sh
brew install ollama
```

Start the server (the macOS app does this for you when it is open):

```sh
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
```

By default it listens on `http://localhost:11434`.

Set `OLLAMA_CONTEXT_LENGTH=32768` before starting Ollama. The default 4K
context is too small for the autonomaton's system prompt (~11K tokens)
and Ollama silently truncates anything larger, dropping the operator
context entirely. 32K leaves room for the system prompt plus turn
history and generation. Going higher — Gemma 4's nominal 128K, for
example — forces Ollama to allocate the full KV cache up front, and on
a Mac that pushes first-token latency past two minutes per turn. Match
the window to the workload, not the model's theoretical max. To make it
persistent, add the export to your shell profile:

```sh
echo 'export OLLAMA_CONTEXT_LENGTH=32768' >> ~/.zshrc
```

## Step 2 — Pull the model

Download the model your Tier 2 binding will name. This guide uses
`gemma4`:

```sh
ollama pull gemma4
```

Confirm it is present:

```sh
ollama list
```

The model name you pull must match the `model:` value you set in Step 3.

## Step 3 — Point Tier 2 at Ollama

Open your config — the operator copy, not the repository seed:

```
~/.grove/routing.config.yaml
```

Find the `T2:` block under `tier_preferences:` and replace it:

```yaml
    T2:
      provider: ollama
      model: gemma4
      description: |
        Daily driver. Most interactions run on-device at zero marginal
        cost. Gemma 4 handles drafting, analysis, code, and conversation.
        The GPU serves the operator; the API bill drops to near-zero for
        routine work.
      max_tokens: 8192
```

Two values do the work: `provider: ollama` and `model: gemma4`. The
endpoint resolves to `http://localhost:11434/v1` automatically — the tier
block carries no `base_url` field.

Leave Tier 1 and Tier 3 on their cloud bindings. The full daily-driver
shape:

```yaml
    T1:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      max_tokens: 4096
    T2:
      provider: ollama
      model: gemma4
      max_tokens: 8192
    T3:
      provider: anthropic
      model: claude-opus-4-6
      max_tokens: 16384
```

## Step 4 — Restart

The router reads `routing.config.yaml` at startup. Exit the autonomaton
and start it again. The new Tier 2 binding takes effect on the next
request.

## Confirm it works

Run a simple request — one that does not need apex reasoning. It routes
to Tier 2. The tier label names the local model, and the cost line for
that turn reads `$0` (local). Ask a hard, multi-step question and the
router escalates that turn to Tier 3 on the cloud apex model, then drops
back to Tier 2 for the next turn. That per-turn movement is the router
working as designed: you pay apex rates only for the turns that need
apex cognition.

To check the endpoint directly:

```sh
curl http://localhost:11434/v1/models
```

It lists the models Ollama is serving. If the model you named is in that
list, Tier 2 will reach it.

## Running Ollama on another host

To run Ollama on a different host or port — another machine on your
network, or a non-default port — set `OLLAMA_BASE_URL` in the
environment before starting the autonomaton:

```sh
export OLLAMA_BASE_URL=http://192.168.1.50:11434/v1
```

Put that line in your shell profile to make it persistent. When
`OLLAMA_BASE_URL` is unset, the binding uses `http://localhost:11434/v1`.

## What stays in the cloud, and why

**Tier 1 — classification.** T1 scores every incoming request: what kind
of work it is, and how confident that judgment is. The v0.1 classifier
is built on the Anthropic Messages protocol, so T1 must stay on an
Anthropic-native provider. Bind T1 to Ollama and classification stops:
every request falls through to default-tier behavior, and the router's
escalation rules never fire. Keep T1 on Haiku.

**Tier 3 — apex cognition.** Hard problems — multi-step planning, novel
synthesis, architecture-level reasoning — escalate to T3. A local model
serves the daily driver well; the apex tier is where a larger cloud
model still earns its cost. Per-turn routing means one turn on the apex
model, then back to local.

The result is a deliberate hybrid: a cheap cloud classifier, a free
local daily driver, and a cloud apex model held in reserve.

## When Ollama is not running

If Ollama is not running when the autonomaton needs Tier 2, the request
fails — loudly. You get a connection error naming the local endpoint,
not a silent empty response. This is intended: a tier that cannot answer
says so, rather than returning nothing and leaving you to guess.

Start the server and retry:

```sh
ollama serve
```

On macOS, opening the Ollama app has the same effect.
