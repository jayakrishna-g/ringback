# What we found running ringback on Runta

All on CLI 0.2.17 and Python SDK 0.2.6, Ubuntu 24.04 image `clean`, 1 vCPU / 1 GiB (max 2 GiB),
`--publish 8080/https`. Measured 2026-11-15 and 2026-11-16, on two runtimes:

* **First runtime:** `--idle-mode suspend-and-wakeup --idle-timeout 120`. Used for the live calls
  and the wake tests.
* **Current runtime:** `--idle-mode disabled --llm-tool-io-capture`, with an egress allowlist.
  Used for the governance features.

Each result below is something I ran and saw. Where I've drawn a conclusion from it, I say so.

## Works

| What | Result |
|---|---|
| Create runtime to `running` | 14 s and 18 s on the two creates |
| Public HTTPS port (`https://8080-<id>.runta.dev`) | Twilio's real webhook and media websocket reach the app (live calls on 2026-11-16) |
| WebSocket upgrade through ingress | Upgrade in 0.34 s. App-level close codes pass through (1008). |
| Secret stub on `api.openai.com`, HTTPS **and** the Realtime WSS handshake | Key injected on both. The voice line runs with no OpenAI key in the runtime. |
| Secret stub on `api.anthropic.com` (`x-api-key`) | Claude Code runs headless in the runtime with a placeholder key |
| Stub **overrides** a header the client already sent | A placeholder `Authorization` still got 200 from OpenAI |
| Secrets not visible inside the runtime | `env` and `runtime.env` don't contain them |
| Processes survive suspend/resume | `uptime -s` unchanged, systemd `NRestarts=0`, SQLite state intact |
| **Egress allowlist** (`runta egress set --mode allowlist`) | With 4 hosts allowed: `example.com` and `pypi.org` time out. The allowed APIs return 200 (stubs still inject). `127.0.0.1` is unaffected. Claude Code runs with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. |
| **LLM tool I/O capture** (`--llm-tool-io-capture`) | `runta tokens analyze --runtime <rt>` lists each Claude Code tool call: command, output, `tool_call_id`, timestamp. A failed `sqlite3: command not found` was recorded too. |
| **Path-scoped secret rules** | With rules only on `/v2/runtimes/*/checkpoints` and `/v2/checkpoints/*`, the runtime can create and poll checkpoints, but `GET /v2/runtimes` and `/v2/secrets` get `403 Unauthenticated` |
| **Live checkpoint** | Ready in 39.3 s, created from my laptop, and 66.7 s, created from inside the runtime through the stub. The runtime kept serving `/health` afterwards. |

## Gets in the way

1. **Inbound traffic did not wake a suspended runtime.** With `suspend-and-wakeup`, requests
   to both `https://8080-<id>.runta.dev` and `https://<id>.runta.dev` got an empty Cloudflare
   **404** in about 0.2 s for over a minute, and the status stayed `suspended`. The docs say
   the wake-up path "restores the runtime and then proxies the original request" (the page is
   marked Preview). For an agent people *call*, this is the deciding feature.
2. **An explicit `runta resume` took 20.6 s to a working `/health`.** Twilio's webhook times
   out at about 15 s. So even if waking worked, a phone call would likely need a front door
   that can hold the line (my inference).
3. **"Idle" means no inbound traffic.** A runtime whose systemd service was running, but
   which had no requests, was suspended. Between my test calls the runtime went to sleep and
   the next call failed, so I kept it awake by polling `/health`. ringback now defaults to
   `--idle-mode disabled`.
4. **`runta exec` kills every process the session started when the session ends.** `nohup`
   and `setsid` didn't survive it. PID 1 is systemd, so I run services as unit files or with
   `systemd-run`. I didn't find this in the docs.
5. **Tool I/O capture can only be set at creation.** `runta run` has `--llm-tool-io-capture`,
   but I found no command to turn it on for an existing runtime (`egress session-content set`
   covers session content only). Turning it on meant recreating the runtime: a new URL, and
   the old disk lost unless you copy it first.
6. **Secret-rule paths are under-documented, and the two commands disagree.**
   * `runta secret authorize --path '*checkpoints*'` accepted the pattern and built a
     dashboard link.
   * `runta secret rule set` rejected the same pattern ("path_pattern must start with '/' or be '*'").
   * `*` matches one path segment only, so `/v2/*checkpoints*` covered neither
     `/v2/runtimes/<id>/checkpoints` nor `/v2/checkpoints/<id>`. Each endpoint needs its own rule.
   * A new rule took a few seconds to take effect: the first requests got 403, and later
     ones got 200.
7. **No API-token scopes.** To checkpoint itself, a runtime needs a full-account Runta key.
   Path rules narrow what the stub sends it to, but not by HTTP method. On
   `/v2/checkpoints/*`, a compromised agent could also `DELETE` a checkpoint. A token limited
   to "checkpoint this runtime" would close that gap.
8. **Secret stubs are outbound-only.** Verifying Twilio's inbound webhook signature needs the
   Twilio auth token inside the runtime.
9. **The SDK lags the CLI.** `runtimes.create` can't set wake-on-traffic idle mode, egress or
   capture, can't list published-port URLs, and can't set secret rules. So `deploy.py` shells
   out to `runta` for those. (The SDK does cover checkpoints.)
10. **Small CLI and docs mismatches:**
    * Suspend passes through a temporary `deleting` status.
    * `runta ps` hides suspended runtimes without `-a`.
    * `runta egress list` and `runta tokens analyze` take no runtime argument (`--runtime` for the latter).
    * The getting-started docs show stub templates with `${credential}`, but the CLI requires
      `${secret}`.
    * The wake docs use `<id>.runta.dev`, while the publish docs use `<port>-<id>.runta.dev`.
