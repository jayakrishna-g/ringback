# Design

## Architecture

```
 your phone ─PSTN─▶ Twilio ─signed webhook + media websocket─▶ ┌──────────── Runta runtime ─────────────┐
                                                              │ ringback serve (FastAPI :8080)          │
                                                              │  ├ voice bridge ◀─▶ OpenAI Realtime ────│─▶ api.openai.com
                                                              │  ├ start_task ─▶ systemd: claude -p …   │
                                                              │  └ /agent/* ◀── Claude Code hooks       │─▶ api.anthropic.com
                                                              │ SQLite: approvals, inbox, audit log     │   (both keys injected by
                                                              │ /workspace/project  (the code it edits) │    Runta secret stubs)
                                                              └─────────────────────────────────────────┘
```

If the owner is already on the line, the agent asks there instead of ringing a busy phone.
Every decision is recorded with the caller's number, the Twilio CallSid, and the words the
speech recogniser heard.

## Why Runta is the substrate

* **The agent is an AI that runs commands, so it must not hold the keys.** The OpenAI and
  Anthropic keys are Runta secrets. A stub adds them only to requests going out to
  `api.openai.com` and `api.anthropic.com`. Claude Code runs with a placeholder key, and the
  stub replaces it on the wire. We checked this: a placeholder header still got a 200.
* **Runta limits what's possible; ringback asks a person in the moment.** Runta's controls are
  set up front: which hosts the runtime can reach (egress allowlist), which credentials it can
  use and on which API paths (secret rules), and an independent record of every tool call
  (tool I/O capture). None of them can stop *this* command *now* and ask a person. That's
  ringback. The two are wired together:
  * with checkpoints on, an approved risky step runs only after a **Runta checkpoint** of the
    whole runtime, so its effects *inside the runtime* (files, the local DB) can be rolled back
    with `runta checkpoint restore`. A checkpoint can't undo remote effects such as a
    force-push or a `DROP` on a remote database;
  * the phone decision (who, their words, the CallSid) sits next to Runta's capture of what
    actually ran.
* **Published HTTPS port.** Twilio's webhook and media websocket go straight into the runtime,
  next to the agent. There's no tunnel and no separate server.
* **A machine that keeps running after you hang up.** Tasks run as systemd units in the
  runtime, so they outlive the call that started them. Approvals and the audit log are on
  the runtime's durable disk.
* **Suspend-and-wakeup** *(the plan; blocked)*. An agent that is waiting for a human would cost
  disk only, and a phone call would wake it. In our test, traffic didn't wake a suspended
  runtime, and resume took about 20 s, longer than Twilio's ~15 s webhook timeout.
  `scripts/deploy.py probe` reproduces both measurements.

## How Claude Code runs

It's **Claude Code itself** (the CLI, from Anthropic's native installer), not the Claude Agent
SDK. It runs headless (`claude -p "<task>"`) **inside the Runta runtime**:

* `ringback/runner.py` runs it as a transient systemd unit, so it keeps going after the call
  that started it ends.
* Runta provides the machine, the public port and the API keys (injected on the wire). The
  Anthropic key never exists inside the runtime.
* ringback connects it to the phone through Claude Code hooks (`PreToolUse` for approvals,
  `PostToolUse` for progress and phone instructions), plus the `ringback ask` and
  `ringback call` commands it can use itself.

## What's real and what's mocked

| Piece | Status |
|---|---|
| Phone in and out: Twilio webhook (signature check, owner allowlist, one-time stream token) and outbound calls via the REST API | **Real**, tested on live calls |
| Voice: Twilio ⇄ OpenAI Realtime (G.711 µ-law passthrough, barge-in, tool calls) | **Real**, tested on live calls |
| Claude Code started by phone (`start_task` → systemd unit → `claude -p`) | **Real**, tested on live calls |
| Claude Code hooks: phone approval before risky Bash (`PreToolUse`); progress log and phone instructions (`PostToolUse`) | **Real**, tested on live calls |
| Claude Code phoning you itself: a question (`ringback ask`), or "done/failed" when the task ends (`ringback call`) | **Real**; the "done" call is in the [recorded run](calls/README.md) |
| Approvals, inbox and audit log (SQLite) and the `ringback` CLI | **Real**, tested |
| Runta: runtime, published HTTPS port, secret stubs for OpenAI **and** Anthropic, systemd services | **Real**, against a live account (see [FINDINGS.md](../FINDINGS.md)) |
| Checkpoint before an approved risky step (Runta API, path-scoped token, fail closed) | **Real**, tested on live calls: ready 48–53 s after the spoken "yes", then the `DROP` ran. Runta's tool I/O capture shows the `DROP` after the checkpoint |
| Egress allowlist (OpenAI, Anthropic, Twilio, Runta only) | **Real**, verified: other hosts time out, Claude Code still works |
| Runta tool I/O capture of Claude Code's tool calls | **Real**, verified with `runta tokens analyze --runtime ringback` |
| `demo/project`, the "production database" Claude Code migrates | **Demo data**: a 500-row SQLite file, but Claude Code really changes it |
| Waking a suspended runtime when a call comes in | **Not working**: traffic didn't wake it, and an explicit resume took about 20 s |
| Detecting voicemail on approval calls | **Not built** (see [SECURITY.md](SECURITY.md)) |

## Layout

```
ringback/voice.py       one call: Twilio <-> OpenAI Realtime, barge-in, tools (status, decide, start_task, instruct)
ringback/app.py         FastAPI: webhook, media websocket, agent API; asks on a live call instead of dialling
ringback/tasks.py       start Claude Code as a systemd unit
ringback/runner.py      run `claude -p` headless, report the result
ringback/checkpoint.py  Runta checkpoint before an approved step (token via path-scoped secret stub)
ringback/store.py       approvals, inbox, event log (SQLite)
ringback/redact.py      strip credentials before anything is stored or spoken
ringback/twilio.py      signature check, TwiML, outbound call
ringback/cli.py         `ringback` command for agents
hooks/                  Claude Code PreToolUse (phone approval, then checkpoint) + PostToolUse (progress, phone instructions)
demo/project/           the repo Claude Code works on (SQLite users DB, MIGRATION.md)
scripts/deploy.py       Runta deploy (SDK + CLI)
scripts/simulate_call.py  Twilio stand-in for testing without a phone
tests/                  fakes for both websockets and for `claude`; no network
docs/                   setup, design, security, recorded calls, the demo slide deck
```
