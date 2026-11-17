"""Deploy ringback onto a Runta runtime.

  python scripts/deploy.py up        create/refresh runtime, upload, start, point Twilio at it
  python scripts/deploy.py claude "<task>"   start Claude Code on a task (same as asking by phone)
  python scripts/deploy.py reset-project     reseed the demo project Claude Code works on
  python scripts/deploy.py probe     measure cold-wake latency: wait for suspend, then time /health
  python scripts/deploy.py logs      tail the server log
  python scripts/deploy.py down      delete the runtime and the checkpoints ringback made

Uses the Runta Python SDK for files/exec/secrets/checkpoints and the `runta` CLI (JSON
output) for what the SDK doesn't expose: idle mode, tool I/O capture, egress, published
ports and secret rules.

Reads local secrets from .env (never uploaded). The OpenAI and Anthropic keys become
Runta secrets injected on the wire, so they never exist inside the runtime. The
optional Runta key for checkpoints is one you authorize yourself; it is injected
only on the checkpoint API paths.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
NAME = os.environ.get("RINGBACK_RUNTIME", "ringback")
REMOTE = "/workspace/ringback"
VENV = "/workspace/venv"
PORT = 8080
# 0 (default) = always on. A suspended runtime can't take a call today (traffic didn't wake it,
# resume takes ~20 s vs Twilio's ~15 s webhook timeout), so suspend is opt-in. See FINDINGS.md.
IDLE_SECS = int(os.environ.get("RINGBACK_IDLE_SECS", "0"))
OPENAI_SECRET = "ringback-openai"
ANTHROPIC_SECRET = "ringback-anthropic"
RUNTA_SECRET = "ringback-runta"  # you add its value via `runta secret authorize` (see README)
# Only checkpoint endpoints get the Runta token. A rule path must start with "/" and `*` matches
# one path segment, so each endpoint needs its own rule: create, then poll until ready.
CHECKPOINT_PATHS = ["/v2/runtimes/*/checkpoints", "/v2/checkpoints/*"]
# Everything the runtime may reach once set up. pypi/installer hosts are only open during `up`.
EGRESS_ALLOW = ["api.openai.com", "api.anthropic.com", "api.twilio.com", "api.runta.com"]
PROJECT = "/workspace/project"
CLAUDE_BIN = "/root/.local/bin/claude"
# Claude Code refuses to start without *some* key; the real one is injected on the wire.
KEY_PLACEHOLDER = "sk-ant-injected-by-runta-secret-stub"
SHIP = ["ringback", "demo", "hooks", "pyproject.toml", "README.md"]
SERVICE_UNIT = f"""[Unit]
Description=ringback voice line
After=network-online.target

[Service]
WorkingDirectory={REMOTE}
EnvironmentFile={REMOTE}/runtime.env
ExecStart={VENV}/bin/ringback serve
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=value lines; `#` starts a comment (whole line, or after whitespace); quotes are stripped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ("'", '"') and v[:1] in v[1:]:
            v = v[1:v.index(v[0], 1)]  # quoted: keep everything inside, including '#'
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        out[k.strip()] = v
    return out


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if path.exists():
        for k, v in parse_dotenv(path.read_text()).items():
            os.environ.setdefault(k, v)


def need(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        sys.exit(f"missing {key} (set it in .env)")
    return val


def runta_cli(*args: str, check: bool = True) -> dict:
    proc = subprocess.run(["runta", *args], capture_output=True, text=True)
    out = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"raw": out}
    if check and (proc.returncode != 0 or "error" in data):
        sys.exit(f"runta {' '.join(args)} failed:\n{out}")
    return data


def find_url(data: object) -> str | None:
    text = json.dumps(data)
    m = re.search(r'https://[^"\s]+', text)
    return m.group(0).rstrip("/") if m else None


def sdk():
    from runta import Runta

    return Runta()


def remote_exec(rt, cmd: str, timeout: float = 600, env: dict | None = None) -> str:
    res = rt.exec(["sh", "-lc", cmd], timeout=timeout, env=env)
    if res.exit_code != 0:
        sys.exit(f"remote command failed ({res.exit_code}): {cmd}\n{res.stdout_text}\n{res.stderr_text}")
    return res.stdout_text


def stage() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ringback-"))
    for item in SHIP:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, tmp / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.exists():
            shutil.copy2(src, tmp / item)
    return tmp


def ensure_runtime(client) -> object:
    existing = {r.name: r for r in client.runtimes.list()}
    if NAME in existing:
        print(f"runtime {NAME} exists; reusing")
        rt = existing[NAME]
        if rt.status().value not in ("running",):
            runta_cli("resume", NAME, check=False)
            runta_cli("boot", NAME, check=False)
            rt.wait_until_ready(timeout=240)
        return rt
    idle = (["--idle-mode", "suspend-and-wakeup", "--idle-timeout", str(IDLE_SECS)] if IDLE_SECS
            else ["--idle-mode", "disabled"])
    print(f"creating runtime {NAME} (1 vCPU, 1 GiB, max 2 GiB, publish {PORT}/https, "
          f"{f'suspend-and-wakeup after {IDLE_SECS}s idle' if IDLE_SECS else 'always on'})")
    t0 = time.time()
    runta_cli("run", "--name", NAME, "--cpus", "1", "--memory", "1024", "--memory-max", "2048",
              "--publish", f"{PORT}/https", *idle,
              # Runta records the agent's LLM tool calls and results: what it actually did,
              # alongside ringback's record of who approved it. Read with `runta tokens analyze`.
              "--llm-tool-io-capture", "--wait")
    print(f"  ready in {time.time() - t0:.1f}s")
    return client.runtimes.get(NAME)


def configure_openai_stub(client) -> None:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("  ! OPENAI_API_KEY not set: skipping secret stub (calls will fail to reach OpenAI)")
        return
    client.secrets.set(OPENAI_SECRET, key)
    runta_cli("secret", "rule", "set", NAME, "--secret", OPENAI_SECRET, "--host", "api.openai.com",
              "--header", "Authorization", "--template", "Bearer ${secret}")
    print("  OpenAI key stored as a Runta secret; stub injects it on api.openai.com")


def configure_anthropic_stub(client) -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("  ! ANTHROPIC_API_KEY not set: Claude Code tasks won't be able to reach Anthropic")
        return False
    client.secrets.set(ANTHROPIC_SECRET, key)
    runta_cli("secret", "rule", "set", NAME, "--secret", ANTHROPIC_SECRET, "--host", "api.anthropic.com",
              "--header", "x-api-key", "--template", "${secret}")
    print("  Anthropic key stored as a Runta secret; stub injects it on api.anthropic.com")
    return True


def setup_claude(rt) -> None:
    """Install Claude Code and the project it works on, with ringback's hooks wired in."""
    print("installing Claude Code + demo project")
    remote_exec(rt, f"test -x {CLAUDE_BIN} || (curl -fsSL https://claude.ai/install.sh | bash) >/dev/null 2>&1; "
                    f"{CLAUDE_BIN} --version", timeout=900)
    remote_exec(rt, f"test -d {PROJECT} || ({reset_project_cmd()})")
    install_project_hooks(rt)


def reset_project_cmd() -> str:
    return (f"rm -rf {PROJECT} && cp -r {REMOTE}/demo/project {PROJECT} && cd {PROJECT} && "
            f"{VENV}/bin/python seed.py && git init -q && git add -A && "
            f"git -c user.name=ringback -c user.email=ringback@localhost commit -qm 'initial' ")


def install_project_hooks(rt) -> None:
    remote_exec(rt, f"mkdir -p {PROJECT}/.claude && cp {REMOTE}/hooks/settings.example.json {PROJECT}/.claude/settings.json")


def configure_runta_stub(client) -> bool:
    """Checkpoints need the Runta API. The token is a secret *you* authorize; we only scope where it goes."""
    names = {s.display_name for s in client.secrets.list()}
    if RUNTA_SECRET not in names:
        print(f"  ! no '{RUNTA_SECRET}' secret: checkpoints before approved steps are OFF. To turn them on,\n"
              f"    create a Runta API key (Dashboard > Settings > Runta API Keys), then run\n"
              f"      runta secret authorize {RUNTA_SECRET} --host api.runta.com --path '{CHECKPOINT_PATHS[0]}' "
              f"--header Authorization --template 'Bearer ${{secret}}'\n"
              f"    open the link it prints, paste the key, and run `deploy.py up` again.")
        return False
    for path in CHECKPOINT_PATHS:
        runta_cli("secret", "rule", "set", NAME, "--secret", RUNTA_SECRET, "--host", "api.runta.com",
                  "--path", path, "--header", "Authorization", "--template", "Bearer ${secret}")
    print(f"  Runta token injected only on api.runta.com {' and '.join(CHECKPOINT_PATHS)}: "
          "checkpoints before approved steps are ON")
    return True


def lock_egress(open_up: bool) -> None:
    if open_up:
        runta_cli("egress", "set", NAME, "--mode", "denylist")
        return
    args = [a for h in EGRESS_ALLOW for a in ("--allow", h)]
    runta_cli("egress", "set", NAME, "--mode", "allowlist", *args)
    print(f"  egress locked to: {', '.join(EGRESS_ALLOW)}")


def point_twilio(public_url: str) -> None:
    if not all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER")):
        print("  ! Twilio settings incomplete: skipping webhook setup")
        return
    sid, token, number = need("TWILIO_ACCOUNT_SID"), need("TWILIO_AUTH_TOKEN"), need("TWILIO_NUMBER")
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    with httpx.Client(auth=(sid, token), timeout=20) as c:
        found = c.get(f"{base}/IncomingPhoneNumbers.json", params={"PhoneNumber": number}).raise_for_status().json()
        if not found["incoming_phone_numbers"]:
            print(f"  ! {number} is not a number on this Twilio account: skipping webhook setup")
            return
        pn_sid = found["incoming_phone_numbers"][0]["sid"]
        c.post(f"{base}/IncomingPhoneNumbers/{pn_sid}.json",
               data={"VoiceUrl": f"{public_url}/twilio/voice", "VoiceMethod": "POST"}).raise_for_status()
    print(f"  Twilio {number} -> {public_url}/twilio/voice")


def up() -> None:
    load_dotenv()
    owners = os.environ.get("RINGBACK_OWNER_NUMBERS", "")
    client = sdk()
    rt = ensure_runtime(client)
    configure_openai_stub(client)
    has_claude = configure_anthropic_stub(client)
    has_checkpoints = configure_runta_stub(client)
    lock_egress(open_up=True)  # installs below need pypi and Anthropic's installer
    try:
        ports = runta_cli("ports", "ls", NAME)
        public_url = find_url(ports)
        if not public_url:
            sys.exit(f"could not find public URL in `runta ports ls` output: {ports}")
        print(f"  public URL: {public_url}")

        print("uploading code")
        remote_exec(rt, f"mkdir -p {REMOTE}")
        rt.files.upload(str(stage()), REMOTE)

        print("installing into runtime venv")
        remote_exec(rt, f"python3 --version && (test -x {VENV}/bin/python || python3 -m venv {VENV}) "
                        f"&& {VENV}/bin/pip install -q -e {REMOTE}", timeout=900)

        agent_token = os.environ.get("RINGBACK_AGENT_TOKEN") or secrets.token_urlsafe(24)
        env_lines = {
            "RINGBACK_PUBLIC_URL": public_url,
            "RINGBACK_DB": f"{REMOTE}/ringback.db",
            "RINGBACK_OWNER_NUMBERS": owners,
            "RINGBACK_AGENT_TOKEN": agent_token,
            "TWILIO_ACCOUNT_SID": os.environ.get("TWILIO_ACCOUNT_SID", ""),
            # Kept in the runtime only to verify inbound webhook signatures (and place calls).
            "TWILIO_AUTH_TOKEN": os.environ.get("TWILIO_AUTH_TOKEN", ""),
            "TWILIO_NUMBER": os.environ.get("TWILIO_NUMBER", ""),
            "RINGBACK_REALTIME_MODEL": os.environ.get("RINGBACK_REALTIME_MODEL", "gpt-realtime"),
            "RINGBACK_CALL_WHEN_DONE": os.environ.get("RINGBACK_CALL_WHEN_DONE", "1"),
            # NOTE: no OPENAI_API_KEY - the secret stub provides it.
            "RINGBACK_PROJECT_DIR": PROJECT if has_claude else "",
            "RINGBACK_CLAUDE_BIN": CLAUDE_BIN,
            "ANTHROPIC_API_KEY": KEY_PLACEHOLDER,  # real key only ever exists in the Runta secret
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no telemetry hosts to allow through egress
            "RINGBACK_RUNTIME_ID": rt.id if has_checkpoints else "",
        }
        rt.files.write(f"{REMOTE}/runtime.env", "".join(f"{k}={v}\n" for k, v in env_lines.items()))
        remote_exec(rt, f"chmod 600 {REMOTE}/runtime.env")
        if has_claude:
            setup_claude(rt)

        # Runta kills everything an exec session started when the session ends (nohup/setsid
        # don't help), and PID 1 is systemd - so the server is a systemd service. That also
        # brings it back after shutdown/boot.
        print("starting server (systemd unit ringback.service)")
        rt.files.write("/etc/systemd/system/ringback.service", SERVICE_UNIT)
        remote_exec(rt, "systemctl daemon-reload && systemctl enable ringback.service >/dev/null 2>&1; "
                        "systemctl restart ringback.service && sleep 2 && systemctl is-active ringback.service")
        for _ in range(20):
            try:
                if httpx.get(f"{public_url}/health", timeout=10).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            sys.exit("server did not answer /health through the public URL; see `deploy.py logs`")
        print("  /health OK through Runta ingress")
    finally:
        # Re-lock even if the install or health check failed: never leave a runtime wide open.
        lock_egress(open_up=False)
    point_twilio(public_url)
    if os.environ.get("TWILIO_NUMBER"):
        print(f"\nDone. Call {os.environ['TWILIO_NUMBER']} from {owners}.")
    print(f"Agent CLI from your laptop:\n  export RINGBACK_URL={public_url} RINGBACK_AGENT_TOKEN={agent_token}")


def claude_task() -> None:
    task = " ".join(sys.argv[2:]).strip()
    if not task:
        sys.exit('usage: deploy.py claude "<task>"')
    rt = sdk().runtimes.get(NAME)
    remote_exec(rt, f"cd {REMOTE} && {VENV}/bin/python -c "
                    f"'import sys; from ringback.tasks import SystemdLauncher; "
                    f"SystemdLauncher(\"{PROJECT}\", \"{REMOTE}/runtime.env\").start(sys.argv[1])' {shlex.quote(task)}")
    print("Claude Code started (unit claude-task); follow it with `deploy.py logs`")


def reset_project() -> None:
    rt = sdk().runtimes.get(NAME)
    remote_exec(rt, reset_project_cmd())
    install_project_hooks(rt)
    print(f"reseeded {PROJECT}")


def logs() -> None:
    rt = sdk().runtimes.get(NAME)
    print(remote_exec(rt, "journalctl -u ringback --no-pager -n 60; echo ---; "
                          "journalctl -u claude-task --no-pager -n 30 || true"))


def probe() -> None:
    """The risky assumption: does a suspended runtime wake fast enough for Twilio's webhook (~15s)?"""
    load_dotenv()
    client = sdk()
    rt = client.runtimes.get(NAME)
    url = find_url(runta_cli("ports", "ls", NAME))
    if not IDLE_SECS:
        sys.exit("probe needs a suspending runtime: deploy.py down, then RINGBACK_IDLE_SECS=120 deploy.py up")
    print(f"waiting for {NAME} to suspend (idle timeout {IDLE_SECS}s)...")
    while (s := rt.status().value) != "suspended":  # passes through a transient "deleting"
        print(f"  status={s}")
        time.sleep(30)

    # 1) Does inbound traffic wake it, as idle mode suspend-and-wakeup promises?
    t0 = time.time()
    codes = []
    while time.time() - t0 < 30:
        codes.append(httpx.get(f"{url}/health", timeout=20).status_code)
        if codes[-1] == 200:
            break
        time.sleep(2)
    dt = time.time() - t0
    if codes[-1] == 200:
        print(f"traffic wake -> 200 in {dt:.2f}s: {'OK' if dt < 15 else 'TOO SLOW'} for Twilio's 15s webhook timeout")
        return
    print(f"traffic did NOT wake it: {len(codes)} requests over {dt:.0f}s, codes {sorted(set(codes))}, "
          f"status={rt.status().value}")

    # 2) Fallback: explicit resume through the API.
    t0 = time.time()
    runta_cli("resume", NAME)
    while httpx.get(f"{url}/health", timeout=10).status_code != 200:
        time.sleep(0.5)
    dt = time.time() - t0
    print(f"explicit resume -> 200 in {dt:.2f}s: {'OK' if dt < 15 else 'TOO SLOW'} for Twilio's 15s webhook timeout")


CHECKPOINT_IDS_SQL = ("SELECT DISTINCT json_extract(data, '$.checkpoint_id') FROM events "
                      "WHERE kind IN ('checkpoint_started', 'checkpoint')")


def own_checkpoint_ids(rt) -> list[str]:
    """IDs of checkpoints *this* runtime's ringback created, from its own event log (never by name)."""
    code = ("import sqlite3, sys\n"
            f"for (cid,) in sqlite3.connect('{REMOTE}/ringback.db').execute(sys.argv[1]):\n"
            "    print(cid or '')")
    res = rt.exec([f"{VENV}/bin/python", "-c", code, CHECKPOINT_IDS_SQL], timeout=60)
    if res.exit_code != 0:
        raise RuntimeError(res.stderr_text.strip() or "could not read the event log")
    return [line.strip() for line in res.stdout_text.splitlines() if line.strip()]


def down() -> None:
    """Delete ringback's checkpoints first, then the runtime that remembers which ones they are.

    Checkpoints outlive their runtime (and keep billing). Their IDs live in the runtime's own
    event log, so if any deletion fails we keep the runtime and you can just run `down` again.
    """
    client = sdk()
    rt = client.runtimes.get(NAME)
    try:
        ids = own_checkpoint_ids(rt)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"couldn't read {NAME}'s checkpoint log ({exc}); not deleting it. "
                 f"Check `runta checkpoint ls`, then delete it with `runta rm {NAME}`.")
    failed = []
    for cp_id in ids:
        try:
            client.checkpoints.delete(cp_id)
            print(f"deleted checkpoint {cp_id}")
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower() or "404" in str(exc):
                print(f"checkpoint {cp_id} already gone")
            else:
                failed.append(cp_id)
                print(f"  ! checkpoint {cp_id}: {exc}")
    if failed:
        sys.exit(f"kept {NAME} so its checkpoint log survives; fix the above and run `down` again")
    rt.delete()
    print(f"deleted {NAME}")


if __name__ == "__main__":
    cmds = {"up": up, "claude": claude_task, "reset-project": reset_project,
            "logs": logs, "probe": probe, "down": down}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
