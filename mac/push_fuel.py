#!/usr/bin/env python3
"""Runs on the Mac Mini at 7:00am and noon (see com.gasfuel.plist).

At 7:00 it reads the truck's fuel level from the GM Truck Remote backend running on
the same machine and saves just that number to the gas-helper repo as data/fuel.json.
At noon it only checks in, keeping the morning's reading. Either push starts the gas
call job on GitHub. The truck backend's token never leaves this machine.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_ENV = Path(os.environ.get("TRUCK_BACKEND_ENV", Path.home() / "SERVER FILES/truck-backend/.env"))
STATUS_URL = os.environ.get("TRUCK_STATUS_URL", "http://localhost:8090/api/status")
REMOTE = "git@github.com:cointugboat8211/gas-helper.git"
REPO = HERE / "repo"
DEPLOY_KEY = HERE / "deploy_key"  # write access to the gas-helper repo only


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def backend_token():
    for line in BACKEND_ENV.read_text().splitlines():
        if line.startswith("API_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit(f"No API_TOKEN in {BACKEND_ENV}")


def read_fuel_percent(attempts=3):
    request = urllib.request.Request(STATUS_URL, headers={"Authorization": f"Bearer {backend_token()}"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.load(response)
            for category in body["result"]["diagnostics"]:
                for element in category.get("diagnosticElements", []):
                    if element.get("name") == "FUEL_LEVEL" and element.get("value") is not None:
                        return float(element["value"])
            raise RuntimeError("status had no fuel level in it")
        except Exception as e:
            log(f"attempt {attempt} failed: {e}")
            if attempt < attempts:
                time.sleep(90)  # GM throttles back-to-back requests
    return None


def git(*args, cwd=REPO):
    ssh = f'ssh -i "{DEPLOY_KEY}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, "GIT_SSH_COMMAND": ssh},
                          capture_output=True, text=True)


def publish(percent):
    """Push the reading; with percent=None, keep the last reading and just check in."""
    if not REPO.exists():
        result = git("clone", "--depth", "1", REMOTE, str(REPO), cwd=HERE)
        if result.returncode:
            sys.exit(f"clone failed: {result.stderr.strip()}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fuel_file = REPO / "data" / "fuel.json"
    for _ in range(3):  # retry if the repo changed underneath us
        git("fetch", "--depth", "1", "origin", "main")
        git("reset", "--hard", "origin/main")
        payload = json.loads(fuel_file.read_text()) if fuel_file.exists() else {}
        if percent is not None:
            payload.update(percent=percent, read_at=now)
        payload["checked_in_at"] = now
        fuel_file.write_text(json.dumps(payload) + "\n")
        git("add", "data/fuel.json")
        git("-c", "user.name=truck", "-c", "user.email=truck@users.noreply.github.com",
            "commit", "-m", "Truck fuel level")
        result = git("push", "origin", "HEAD:main")
        if result.returncode == 0:
            return
        log(f"push failed, retrying: {result.stderr.strip()[:200]}")
        time.sleep(10)
    sys.exit("Couldn't push the fuel level")


if __name__ == "__main__":
    if datetime.now().hour < 10 or "--read" in sys.argv:
        fuel = read_fuel_percent()
        publish(fuel)  # still checks in if the truck didn't answer, so the gas call goes out
        log(f"published fuel level {fuel:.0f}%" if fuel is not None
            else "couldn't read the fuel level; checked in without it")
    else:
        publish(None)
        log("checked in (no truck request)")
