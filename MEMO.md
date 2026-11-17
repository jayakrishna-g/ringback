# ringback: a background agent that phones you when it needs you

## What to build

A coding agent that keeps working in a Runta runtime after you walk away, and **calls your
phone only when it needs you**:

* before a risky command, it reads you the exact command and you say yes or no;
* when it's stuck on a decision only you can make, it asks;
* when it has finished or failed, it tells you.

You can call it the same way, to start work ("finish the users migration"), to ask where it
is, or to redirect it. Every decision is logged with the caller's number, the Twilio CallSid,
and what the speech recogniser heard. The slice I built runs real Claude Code inside the
runtime.

## Why

Agents running for hours, which is the work Runta is built for, have a few moments where a
human must say yes. Today owners pick one of these:

1. **Babysit.** Stay at the keyboard and approve prompts, which defeats running the agent
   remotely.
2. **Pre-approve everything** (`--dangerously-skip-permissions`). Nobody can show who allowed
   the destructive step.
3. **Come back later.** The agent sits blocked for hours, billing the whole time.

A phone call reaches someone away from the keyboard, with no app and no login. It addresses
two of Runta's themes:

* **governance and proof**: only allowlisted numbers decide, and each decision carries
  evidence;
* **handoffs**: agent and person switch control in under a minute, and the agent keeps its
  place.

**Why now:** speech-to-speech models take phone audio (G.711 µ-law) directly. Our calls
answered in 0.6–2.3 s, fast enough to feel like a conversation.

**Why Runta, and how it fits with Runta's governance:** Runta's controls are set before the
run:
* egress: which hosts the runtime can reach;
* secret rules: which keys it can use, and on which API paths;
* capture: an independent record of every tool call.

None of them can stop *this* command *now* and ask a person. ringback fills that gap, on top
of those controls:
* Claude Code runs with no API keys; stubs add them on the wire.
* Egress is locked to four APIs.
* With checkpoints on, an approved risky step runs only after a Runta checkpoint, so its
  effects inside the runtime can be rolled back.
* The phone decision sits next to Runta's capture of what actually ran.

## Evidence

This comes from my own use and from measurements. I didn't interview other developers in the
time available; that's the first follow-up.

* **The live test worked** (2026-11-16, from the runtime's event log):
  1. I called and said "finish the users migration", and the line read it back.
  2. Claude Code backed up the DB, backfilled 40 emails, and edited `app.py`.
  3. At `ALTER TABLE users DROP COLUMN legacy_email`, the hook held the command and my phone
     rang.
  4. I said "yes", and the DROP ran.
  5. Claude Code finished all five steps.

  The whole thing took about two minutes.
* **What the live calls found**, all fixed except the last:
  * the transcriber heard English as Persian, so the line switched to Indonesian (I pinned
    English);
  * barge-in sent OpenAI an invalid truncate request (stale clock; fixed with a regression
    test);
  * `rm -rf __pycache__` rang my phone (cache folders now exempt);
  * the approval callback first hit my voicemail greeting (not fixed; see "Cut").
* **The governance pieces, verified on Runta:**
  * with egress locked, other hosts time out, and Claude Code still works;
  * `runta tokens analyze` lists each command Claude Code ran, with its output;
  * the runtime checkpointed itself in 66.7 s through a secret rule scoped to checkpoint
    paths, and `GET /v2/runtimes` from the same runtime got 403.
* **What blocks Runta's best case** (details in FINDINGS.md):
  * traffic did **not** wake a suspended runtime;
  * an explicit resume took 20.6 s, longer than Twilio's ~15 s webhook timeout;
  * a runtime with a running service but no requests counts as idle and is suspended;
  * `runta exec` kills the processes it started, so services need systemd.

## Shape of the solution

* **One server inside the runtime** takes Twilio's webhook and media websocket and bridges the
  call to OpenAI Realtime.
* **The voice model has four tools:** status, decide an approval, start a task, and send an
  instruction. Each decision must use up one caller turn spoken after the request, and any
  hook error denies.
* **Claude Code runs as a systemd unit**, so it outlives the call. It phones you itself
  (`ringback ask` for a question, `ringback call` when done). Two hooks connect it to the
  phone:
  * `PreToolUse` holds risky Bash commands for a phone decision. If you're already on the
    line, it asks there. On "yes" it checkpoints the runtime first; if the checkpoint fails,
    the command doesn't run.
  * `PostToolUse` logs progress and passes your phone instructions to Claude Code before its
    next step.
* **Security:**
  * the webhook signature is checked;
  * only owner numbers are accepted, inbound and outbound;
  * each media stream needs a one-time token;
  * each approval can be decided once.
* **120 tests, none needing the network:** fakes for both websockets, Runta's API and the
  `claude` binary.

## What I cut

* **Waking the runtime on a call.** Tested; it doesn't work today. An always-on front door
  that answers "one moment" while it resumes the runtime would work, but it belongs in Runta's
  wake path, not in every app.
* **Protection from a hostile agent.** The approval records live in the agent's own runtime, so
  a hostile agent could forge one. ringback is built for a cooperative agent that makes
  mistakes. The fix is to move approvals outside the agent's runtime.
* **Voicemail and spoofing protection.** Answering-machine detection is a few lines but adds
  about 3 s per call. A spoken PIN would cover spoofed caller ID.
* **More than one task, owners or channel.** No task queue, no escalation chains, and no SMS or
  Slack fallback.

## Following up after shipping

**Signals it's working:**
* median time from the agent asking to a human deciding (minutes, not hours);
* the share of approvals decided rather than expired;
* the share of calls answered;
* runtimes that drop skip-all permissions;
* the hours a blocked agent sits billing.

**Signals it isn't, and what I'd change:**
* **Unanswered calls or quick hang-ups:** batch approvals, add quiet hours, fall back to SMS.
* **Approvals later regretted:** review the stored transcripts and make the read-back stricter.
* **Owners muting it:** tighten the risk filter. The `__pycache__` call was an early example.

**What I'd ask Runta for:**
* wake-on-traffic that holds the request and is ready in under 10 s;
* an idle signal the agent can set;
* inbound signature verification, so the Twilio token can stay outside the runtime;
* scoped API tokens ("checkpoint this runtime"). Path rules can't restrict the HTTP method.
