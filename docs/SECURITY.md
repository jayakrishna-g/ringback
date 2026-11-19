# Threat model and known gaps

ringback assumes a **cooperative agent that makes mistakes**, not a hostile one. That's the
everyday case: Claude Code about to run a destructive command it shouldn't run unattended.

The risky-command check is a pattern filter. It catches `DROP COLUMN`, `rm -r`, force-push,
`terraform apply` and their usual spellings, but it is not a sandbox.

## What is enforced in code

* A gated command runs only if the hook gets a recorded "approved" decision for it. Any error,
  timeout, malformed answer or failed checkpoint means **deny**.
* A decision or a task start must **use up one caller turn** that came after the request
  existed, and older unused turns are retired with it. One "yes" buys one action, so earlier
  small talk can't approve a backlog. A task starts in two steps: the line registers it and
  reads it back, and only a caller turn *after* that starts exactly the registered wording. The
  voice model can't act on its own, or because agent text told it to. Each decision records the
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

## What is not enforced: a hostile agent can get around it

The agent runs as root in the same runtime as the approval database, the hook scripts and the
Twilio token. It could write "approved" into SQLite, edit the hook, or hide a destructive
command where a pattern filter can't see it (`python -c`, `eval "$X"`, a script file). Closing
that means running the approval service and its records outside the agent's runtime, and
running Claude Code as an unprivileged user. That's the next step, and Runta's runtime
isolation is the natural place for it.

## Known gaps

* **Voicemail.** On both live tests, the approval call first reached the owner's voicemail
  greeting. On the second, the line read the request to the greeting for about 70 s and
  recorded no decision, then got the owner's "yes" once they picked up. But a stray "yes" in a
  greeting could still count. Twilio answering-machine detection (`MachineDetection=Enable`,
  then drop the call when `AnsweredBy` is a machine) would close this, at a cost of about 3 s
  of setup per call.
* **Caller ID can be spoofed,** and a caller turn proves *that* someone spoke, not *who*, or
  that they said yes. The model interprets the words. High-risk approvals should also need a
  PIN, or keypad (DTMF) confirmation.
* **"Only a clear yes" is enforced in the voice model's instructions, not in code.** On the
  recorded run ([calls](calls/README.md)), a task started on "Mhm." while the model reported
  "Yes, go ahead." The log keeps both, so it's visible after the fact. Next fix: check the
  recognised words at decision time, and ask again when they aren't a clear yes.
* **Replay protection is in memory.** A server restart forgets which CallSids already had a
  stream. Replaying also requires capturing a signed request, which travels over TLS.
* **Phone instructions aren't acknowledged.** If a hook dies after taking an instruction but
  before handing it to Claude Code, that instruction is lost.
* **Stubs are outbound-only.** To verify Twilio's inbound signatures, the Twilio auth token has
  to live inside the runtime.
* **The checkpoint token is full-account, only path-scoped.** The agent can't read it, and it
  only reaches checkpoint endpoints. But it isn't limited by HTTP method, so a compromised
  agent could delete a checkpoint. Runta has no token scopes yet.
* **Checkpoints add time.** A checkpoint took 39–67 s, during which the approved command waits.
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
