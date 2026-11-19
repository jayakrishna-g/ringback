# Recorded calls (18 Nov 2026)

Three real calls from one end-to-end run, recorded on the owner's phone. Each `.mp4` is the
call audio with captions. Captions come from the server's own log (what the line said) and a
local speech-to-text pass (timings, and what the owner said).

| Clip | What happens | Length |
|---|---|---|
| [call-1-start-task.mp4](call-1-start-task.mp4) | The owner asks for the migration; the line reads it back and starts Claude Code | 0:30 |
| [call-2-approval.mp4](call-2-approval.mp4) | ringback calls before `DROP COLUMN legacy_email`; the owner asks what it will run, then approves | 0:50 |
| [call-3-done.mp4](call-3-done.mp4) | ringback calls back: done, 40 emails copied, nothing committed yet | 0:32 |

## What the server logged for the same run (UTC)

| Time | Event |
|---|---|
| 04:25:23 | `task_proposed`: the line registers the task, then reads it back |
| 04:25:27 | `task_requested`: started on the caller turn that followed the read-back |
| 04:25:58 | `approval_requested` for the `DROP`, and the owner's phone is dialled |
| 04:26:48 | `approval_approved`: caller turn transcript "Yes." |
| 04:26:50 → 04:27:41 | Runta checkpoint `before-a04f9021-…`, ready in 53.2 s; then the `DROP` ran |
| 04:27:51 | `task_done`, and the owner is called with the summary |

## One thing these recordings show

In call 1, the turn that started the task was a **"Mhm."** The voice model reported the
owner's words as "Yes, go ahead." The server logged the speech recogniser's "Mhm." right next
to the model's account, which is why it keeps both. Today, "only a clear yes" is enforced in the
voice model's instructions. Checking the recognised words in code, and asking again when they
aren't a clear yes, is the next fix. See "Known gaps" in [SECURITY.md](../SECURITY.md).

The phone's own "This call will be recorded" announcement also reached the line as a caller
turn. It was harmless here, because a turn alone never approves anything that came after it.
