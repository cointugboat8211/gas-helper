#!/usr/bin/env python3
"""The daily job. Runs on the Mac Mini at 7:00am and noon (see com.gasfuel.plist).

It lives inside a clone of the gas-helper repo and does everything itself:
  1. pulls the latest code and history from GitHub,
  2. at 7:00, reads the truck's fuel level from the GM Truck Remote backend on this machine,
  3. runs gas.py (fetch prices, make the call, send the notification, rebuild the page),
  4. pushes the updated history and page back to GitHub, which hosts the page.

The truck backend's token never leaves this machine. Pass --no-notify to do a silent run,
or --read to force a fuel reading outside the morning.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY_KEY = REPO.parent / "deploy_key"  # write access to the gas-helper repo only
BACKEND_ENV = Path(os.environ.get("TRUCK_BACKEND_ENV", Path.home() / "SERVER FILES/truck-backend/.env"))
STATUS_URL = os.environ.get("TRUCK_STATUS_URL", "http://localhost:8090/api/status")


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


def backend_token():
    for line in BACKEND_ENV.read_text().splitlines():
        if line.startswith("API_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError(f"No API_TOKEN in {BACKEND_ENV}")


def read_fuel_percent(attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(STATUS_URL, headers={"Authorization": f"Bearer {backend_token()}"})
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.load(response)
            for category in body["result"]["diagnostics"]:
                for element in category.get("diagnosticElements", []):
                    if element.get("name") == "FUEL_LEVEL" and element.get("value") is not None:
                        return float(element["value"])
            raise RuntimeError("status had no fuel level in it")
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
