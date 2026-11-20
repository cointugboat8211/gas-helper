#!/usr/bin/env python3
"""The daily job. Runs on the Mac Mini at 7:00am and noon (launchd on macOS, a systemd
timer on Linux).

It lives inside a clone of the gas-helper repo and does everything itself:
  1. pulls the latest code and history from GitHub,
  2. at 7:00, reads the truck's fuel level straight from GM's OnStar API (mac/onstar.py),
  3. runs gas.py (fetch prices, make the call, send the notification, rebuild the page),
  4. pushes the updated history and page back to GitHub, which hosts the page.

The OnStar tokens live outside the repo (see onstar.py) and never leave this machine.
Pass --no-notify to do a silent run, or --read to force a fuel reading outside the morning.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onstar  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEPLOY_KEY = REPO.parent / "deploy_key"  # write access to the gas-helper repo only


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def git(*args):
    ssh = f'ssh -i "{DEPLOY_KEY}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
    return subprocess.run(["git", *args], cwd=REPO, env={**os.environ, "GIT_SSH_COMMAND": ssh},
                          capture_output=True, text=True)


def sync():
    """Bring in the latest code and data, keeping any earlier run that failed to push."""
    git("fetch", "origin", "main")
    if git("rebase", "-X", "theirs", "origin/main").returncode:
        git("rebase", "--abort")
        git("reset", "--hard", "origin/main")


def read_fuel_percent(attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            return onstar.read_fuel_percent()
        except onstar.ReauthNeeded as e:
            log(f"fuel reading needs new OnStar tokens (run gm-token-tool, copy its two JSON "
                f"files to {onstar.ONSTAR_DIR}): {e}")
            return None  # retrying can't fix this
        except Exception as e:
            log(f"fuel reading attempt {attempt} failed: {e}")
            if attempt < attempts:
                time.sleep(90)  # GM throttles back-to-back requests
    return None


def publish():
    git("add", "-A", "data", "docs")
    git("-c", "user.name=gas-helper", "-c", "user.email=gas-helper@users.noreply.github.com",
        "commit", "-m", "Daily update")
    for _ in range(3):
        if git("push", "origin", "HEAD:main").returncode == 0:
            return True
        time.sleep(10)
        sync()  # something else pushed meanwhile; put our commit on top and retry
    return False


if __name__ == "__main__":
    sync()
    if datetime.now().hour < 10 or "--read" in sys.argv:
        fuel = read_fuel_percent()
        if fuel is not None:
            reading = {"percent": fuel, "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            (REPO / "data" / "fuel.json").write_text(json.dumps(reading) + "\n")
            log(f"truck fuel level {fuel:.0f}%")

    mode = ["--no-notify"] if "--no-notify" in sys.argv else ["--scheduled"]
    result = subprocess.run([sys.executable, "gas.py", "run", *mode], cwd=REPO, capture_output=True, text=True)
    log((result.stdout + result.stderr).strip() or "gas.py printed nothing")

    log("pushed to GitHub" if publish() else "COULD NOT PUSH to GitHub; will retry on the next run")
