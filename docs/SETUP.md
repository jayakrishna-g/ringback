# Setup and operation

## 1. Run the tests (2 minutes, no accounts)

```bash
git clone https://github.com/jayakrishna-g/ringback.git && cd ringback
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q        # 120 tests, no network, no keys
```

## 2. The full loop on a real phone (about 15 minutes)

You need:

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

## 3. Test it with your phone

1. Call your Twilio number. On a trial account, press a key after Twilio's message.
2. Say **"Start a task: finish the users migration in MIGRATION.md."** Confirm with "yes"
   after the read-back, then hang up.
3. About a minute later it **calls you** to approve `ALTER TABLE users DROP COLUMN legacy_email`.
   Say yes or no. On yes (and with checkpoints on), it checkpoints the runtime first, then
   drops the column.
4. It **calls again when it's done** with a summary. Call it any time to ask how it's going.

To see Runta's own record of what Claude Code ran, use `runta tokens analyze --runtime ringback`.
To list restore points, use `runta checkpoint ls`.

## Operating it

| Command | What it does |
|---|---|
| `deploy.py up` | create or refresh the runtime, upload, start, point Twilio at it |
| `deploy.py claude "<task>"` | start Claude Code on a task, same as asking by phone |
| `deploy.py reset-project` | reseed the demo project and its database between runs |
| `deploy.py logs` | the server's and Claude Code's logs |
| `deploy.py probe` | reproduce the suspend/wake measurements in [FINDINGS.md](../FINDINGS.md) |
| `deploy.py down` | delete this runtime's checkpoints, then the runtime (stops billing) |

The runtime is **always on** by default (`RINGBACK_IDLE_SECS=0`), because a suspended runtime
can't take a call today. Set `RINGBACK_IDLE_SECS=120` to try suspend, then run `deploy.py probe`.

**No Twilio number?** `deploy.py up` skips the phone setup. You can still start Claude Code with
`deploy.py claude "<task>"`, and `scripts/simulate_call.py` stands in for Twilio: it sends a
signed webhook, streams caller lines generated with TTS as 8 kHz µ-law, and writes `call.wav`.
Measured with it: greeting in about 1–1.8 s, replies 0.6–2.3 s after the caller stops.

## Using it from any agent

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
