# ringback

**A coding agent that keeps working in the background, and phones you when it needs you.**

ringback runs Claude Code on a [Runta](https://runta.com) runtime and gives it a phone number.
You give it work by phone, hang up, and get on with your day. It works on its own and calls
you only when it needs you:

* **before a risky command.** It holds commands like `DROP COLUMN`, `rm -r`, force-push
  or `terraform apply` and reads them to you. You say yes or no. (This is a pattern filter
  for a cooperative agent, not a sandbox; see "Threat model".)
* **when it's stuck on a decision only you can make.** It asks "Should I use the staging DB?"
  and acts on your answer.
* **when it's finished, or has failed.** You get a 2–3 sentence summary.

You can also call it any time to ask "what are you doing?" or to redirect it ("skip the
changelog"). If you're already on the line, it asks there instead of ringing a busy phone.
Every decision is recorded with the caller's number, the Twilio CallSid, and the words the
speech recogniser heard.

## Quickstart for evaluators

**1. Without any accounts (2 minutes): run the tests.**

```bash
git clone https://github.com/jayakrishna-g/ringback.git && cd ringback
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q        # 120 tests, no network, no keys
```

**2. The full loop on a real phone (about 15 minutes).** You need:

| Account | What for | Notes |
|---|---|---|
| [Runta](https://runta.com) | the runtime | New accounts include free credits |
| [Twilio](https://console.twilio.com) | the phone number | A trial works. Verify your own phone, then get a number (about $1.15/month). |
| [OpenAI](https://platform.openai.com) | the voice (`gpt-realtime`) | |
| [Anthropic](https://console.anthropic.com) | Claude Code | |
| Runta API key (optional) | checkpoints before approved steps | Dashboard → Settings → Runta API Keys. `deploy.py up` prints the one command to authorize it; without it, checkpoints stay off |

```bash
npm i -g @runta/runta-cli && runta login
cp .env.example .env                 # fill in the 4 keys, your Twilio number, your own phone
.venv/bin/python scripts/deploy.py up   # runtime, keys -> Runta secrets, Claude Code, demo repo, egress lock, Twilio webhook
```

**3. Test it with your phone:**
1. Call your Twilio number. On a trial account, press a key after Twilio's message.
2. Say **"Start a task: finish the users migration in MIGRATION.md."** Confirm with "yes",
   then hang up.
3. About a minute later it **calls you** to approve `ALTER TABLE users DROP COLUMN legacy_email`.
   Say yes or no. On yes (and with checkpoints on), it checkpoints the runtime first, then
   drops the column.
4. It **calls again when it's done** with a summary. Call it any time to ask how it's going.

To see Runta's own record of what Claude Code ran, use `runta tokens analyze --runtime ringback`.
To list restore points, use `runta checkpoint ls`.

Between runs, use `deploy.py reset-project` to reseed the demo DB. `deploy.py logs` shows
what happened. When you're finished, run **`deploy.py down`**, which deletes the runtime and
stops billing.

No Twilio number? `deploy.py up` skips the phone setup. You can still run
`deploy.py claude "<task>"` to start Claude Code, and `scripts/simulate_call.py` stands in
for Twilio against the deployed voice line.

## How it works

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

## Tested on a live call (2026-11-16)

From the runtime's event log:

1. **14:18:31**: owner calls.
2. **14:19:07**: "Start a task: finish the users migration." The line reads it back, and Claude Code starts.
3. **14:19:11–24**: Claude Code backs up the DB, backfills 40 emails, and edits `app.py`.
4. **14:19:28**: Claude Code tries `ALTER TABLE users DROP COLUMN legacy_email`. The hook holds it, and the owner's phone rings.
5. **14:19:54**: the owner says "Yes." The approval is recorded, and the DROP runs.
6. **14:20:23**: Claude Code finishes all 5 steps, and its summary is logged for the next call.
   (Calling the owner when the task ends was added after this run.)

## What's real and what's mocked

| Piece | Status |
|---|---|
| Phone in and out: Twilio webhook (signature check, owner allowlist, one-time stream token) and outbound calls via the REST API | **Real**, tested on live calls |
| Voice: Twilio ⇄ OpenAI Realtime (G.711 µ-law passthrough, barge-in, tool calls) | **Real**, tested on live calls |
| Claude Code started by phone (`start_task` → systemd unit → `claude -p`) | **Real**, tested on a live call |
| Claude Code hooks: phone approval before risky Bash (`PreToolUse`); progress log and phone instructions (`PostToolUse`) | **Real**, tested on a live call |
| Claude Code phoning you itself: a question (`ringback ask`), or "done/failed" when the task ends (`ringback call`) | **Real**, unit-tested; the "done" call was not part of the recorded live run |
| Approvals, inbox and audit log (SQLite) and the `ringback` CLI | **Real**, tested |
| Runta: runtime, published HTTPS port, secret stubs for OpenAI **and** Anthropic, systemd services | **Real**, against a live account (see [FINDINGS.md](FINDINGS.md)) |
| Checkpoint before an approved risky step (Runta API, path-scoped token, fail closed) | **Real**, tested on a live call (2026-11-17): phone "yes" at 09:14:25, checkpoint ready 48.1 s later, then the `DROP` ran. Runta's tool I/O capture shows the `DROP` after the checkpoint |
| Egress allowlist (OpenAI, Anthropic, Twilio, Runta only) | **Real**, verified: other hosts time out, Claude Code still works |
| Runta tool I/O capture of Claude Code's tool calls | **Real**, verified with `runta tokens analyze --runtime ringback` |
| `demo/project`, the "production database" Claude Code migrates | **Demo data**: a 500-row SQLite file, but Claude Code really changes it |
| Waking a suspended runtime when a call comes in | **Not working**: traffic didn't wake it, and an explicit resume took about 20 s |
| Detecting voicemail on approval calls | **Not built**: if voicemail answers, a stray "yes" in the recording could count as approval (see Known gaps) |

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
  * with checkpoints on, an approved risky step runs only after a **Runta checkpoint** of the whole runtime, so its
    effects *inside the runtime* (files, the local DB) can be rolled back with
    `runta checkpoint restore`. A checkpoint can't undo remote effects such as a force-push or a
    `DROP` on a remote database;
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

The runtime is **always on** by default (`RINGBACK_IDLE_SECS=0`), because a suspended runtime
can't take a call today. Set `RINGBACK_IDLE_SECS=120` to try suspend, and run
`deploy.py probe` to reproduce the wake measurements in [FINDINGS.md](FINDINGS.md).

**Simulator.** `scripts/simulate_call.py` plays Twilio's part against the deployed server:
a signed webhook, then caller lines generated with TTS and streamed as 8 kHz µ-law. It writes
`call.wav`. Measured: greeting in about 1–1.8 s, and replies about 0.6–2.3 s after the caller
stops.

### Using it from any agent

Claude Code uses the hooks in `hooks/settings.example.json`. Any other agent that can run a
shell command can use the CLI:

```bash
ringback task "Migrate users table"
ringback log "Backfill done: 1.2M rows"
ringback ask "Drop column legacy_email on prod?" --context "checksums match"   # phones you; exit 0 yes / 1 no / 2 no answer
ringback call "Done: migration finished, 40 rows backfilled"                   # phones you with an update
ringback inbox      # instructions the owner gave by phone (JSON)
ringback status
```

## Threat model

ringback assumes a **cooperative agent that makes mistakes**, not a hostile one. That's the
everyday case: Claude Code about to run a destructive command it shouldn't run unattended.

**What is enforced in code:**
* A gated command runs only if the hook gets a recorded "approved" decision for it. Any error,
  timeout, malformed answer or failed checkpoint means **deny**.
* A decision or a task start must **use up one caller turn** that came after the request
  existed, and older unused turns are retired with it. One "yes" buys one action, so earlier
  small talk can't approve a backlog. A task starts in two steps: the line registers it and
  reads it back, and only a caller turn *after* that starts exactly the registered wording. The voice
  model can't act on its own, or because agent text told it to. Each decision records the
  audio turn behind it, and that turn's transcript is attached when speech recognition
  finishes, even if that happens after the decision.
* Agent text read out on a call is quoted as data, not given as instructions.
* Each Twilio call gets one media stream, bound to its CallSid. A Twilio retry before the
  stream starts gets the same grant back, and a replay after that gets nothing.
* Credentials are redacted before anything is stored (events, task, approval notes, phone
  instructions) or shown to the voice model, without hiding a command's targets. Approval
  text that had something hidden says so.
* Tools run one at a time off the audio loop, so a slow `systemctl` or SQLite call doesn't
  hold up audio, barge-in or transcripts.

**What is not enforced: a hostile agent can get around it.** The agent runs as root in the
same runtime as the approval database, the hook scripts and the Twilio token. It could write
"approved" into SQLite, edit the hook, or hide a destructive command where a pattern filter
can't see it (`python -c`, `eval "$X"`, a script file). Closing that means running the approval service and its records
outside the agent's runtime, and running Claude Code as an unprivileged user. That's the next
step, and Runta's runtime isolation is the natural place for it.

## Known gaps

* **Voicemail.** On both live tests, the approval call first reached the owner's voicemail
  greeting. On the second, the line read the request to the greeting for about 70 s and
  recorded no decision, then got the owner's "yes" once they picked up. But a stray "yes" in a
  greeting could still count. Twilio answering-machine detection (`MachineDetection=Enable`, then drop the call
  when `AnsweredBy` is a machine) would close this, at a cost of about 3 s of setup per call.
* **Caller ID can be spoofed,** and a caller turn proves *that* someone spoke, not *who*, or
  that they said yes. The model interprets the words. High-risk approvals should also need a
  PIN, or keypad (DTMF) confirmation.
* **Replay protection is in memory.** A server restart forgets which CallSids already had a
  stream. Replaying also requires capturing a signed request, which travels over TLS.
* **Phone instructions aren't acknowledged.** If a hook dies after taking an instruction but
  before handing it to Claude Code, that instruction is lost.
* **Stubs are outbound-only.** To verify Twilio's inbound signatures, the Twilio auth token has
  to live inside the runtime.
* **The checkpoint token is full-account, only path-scoped.** The agent can't read it, and it
  only reaches checkpoint endpoints. But it isn't limited by HTTP method, so a compromised
  agent could delete a checkpoint. Runta has no token scopes yet.
* **Checkpoints add time.** A checkpoint took 39–67 s (48 s on the live rehearsal), during which the approved command waits.
  That's fine for a `DROP` but too slow to use on every command.
* **Redaction is pattern-based.** It catches the usual credential shapes (`KEY=value`,
  `--password x`, `-pX` for mysql, `Bearer …`, known key formats). A secret in an unusual shape
  can get through, and a *target* named like a credential (`rm -rf token=production`) is read
  out as `token=[REDACTED]`, with "(credentials hidden)" telling the owner something was cut.
* **Live-call announcements aren't acknowledged.** If the owner is on the line, an approval or
  update is injected there. A failed send falls back to dialling, but a send that succeeds and
  is then dropped by the voice model isn't detected. The approval still stands, and its
  timeout denies.
* **Teardown races a checkpoint in flight.** `deploy.py down` lists this runtime's checkpoints,
  then deletes them. A checkpoint created during teardown is left behind. Stop the agent first.
* **One Claude Code task at a time,** by design. The line refuses a second task while one is running.

## Layout

```
ringback/voice.py    one call: Twilio <-> OpenAI Realtime, barge-in, tools (status, decide, start_task, instruct)
ringback/app.py      FastAPI: webhook, media websocket, agent API; asks on a live call instead of dialling
ringback/tasks.py    start Claude Code as a systemd unit
ringback/runner.py   run `claude -p` headless, report the result
ringback/checkpoint.py  Runta checkpoint before an approved step (token via path-scoped secret stub)
ringback/store.py    approvals, inbox, event log (SQLite)
ringback/twilio.py   signature check, TwiML, outbound call
ringback/cli.py      `ringback` command for agents
hooks/               Claude Code PreToolUse (phone approval, then checkpoint) + PostToolUse (progress, phone instructions)
demo/project/        the repo Claude Code works on (SQLite users DB, MIGRATION.md)
scripts/deploy.py    Runta deploy (SDK + CLI)
scripts/simulate_call.py  Twilio stand-in for testing without a phone
tests/               fakes for both websockets and for `claude`; no network
```
