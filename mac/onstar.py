#!/usr/bin/env python3
"""Reads the truck's fuel level straight from GM's OnStar API. Standard library only.

This replaces the old GM Truck Remote backend (truck-backend, Node + onstarjs2). It does the
same three steps onstarjs2 did, minus everything else:
  1. Microsoft token: reuse it, or refresh it with the stored refresh token
     (custlogin.gm.com B2C). The refresh token rotates, so the new one is saved at once.
  2. GM API token: reuse it, or exchange the Microsoft token for one (na-mobile-api.gm.com).
  3. GET the vehicle health status and pick out FUEL_LEVEL.

There is no password login here. GM blocks automated browser logins, so the first token pair
comes from the gm-token-tool bootstrap (see truck-backend's README). If the refresh token
ever stops working, run that tool again and copy its two JSON files into ONSTAR_DIR.

ONSTAR_DIR (default: <gas-fuel>/onstar, outside the repo so nothing is ever pushed) holds:
  onstar.env              ONSTAR_VIN=..., ONSTAR_DEVICE_ID=... (same device id the tokens were made with)
  microsoft_tokens.json   rewritten on every refresh
  gm_tokens.json          rewritten when the GM token is renewed (~every 30 min of use)

Run by hand:  python3 mac/onstar.py      (prints the fuel level, sends nothing)
"""
import base64
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ONSTAR_DIR = Path(os.environ.get("ONSTAR_DIR", REPO.parent / "onstar"))

MS_POLICY = "https://custlogin.gm.com/gmb2cprod.onmicrosoft.com/b2c_1a_seamless_mobile_signuporsignin"
MS_DISCOVERY_URL = MS_POLICY + "/v2.0/.well-known/openid-configuration"
MS_TOKEN_URL = MS_POLICY + "/oauth2/v2.0/token"  # fallback if discovery fails (as of 2026-11)
MS_CLIENT_ID = "3ff30506-d242-4bed-835b-422bf992622e"  # the myChevrolet app's public client id
GM_TOKEN_URL = "https://na-mobile-api.gm.com/sec/authz/v3/oauth/token"
HEALTH_URL = "https://na-mobile-api.gm.com/api/v1/vh/vehiclehealth/v1/healthstatus/{vin}"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_8_3 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/15.6.6 Mobile/15E148 Safari/604.1")
SKEW = 5 * 60  # renew tokens this many seconds before they expire, like onstarjs2


class ReauthNeeded(RuntimeError):
    """The refresh token was rejected. Only the gm-token-tool bootstrap can fix this."""


def _config():
    env = {}
    path = ONSTAR_DIR / "onstar.env"
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    for key in ("ONSTAR_VIN", "ONSTAR_DEVICE_ID"):
        if not env.get(key):
            raise RuntimeError(f"{key} missing from {path}")
    return env["ONSTAR_VIN"].upper(), env["ONSTAR_DEVICE_ID"]


def _load(name):
    return json.loads((ONSTAR_DIR / name).read_text())


def _save(name, data):
    """Write atomically: a half-written token file would lose the rotated refresh token."""
    path = ONSTAR_DIR / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _http(url, data=None, headers=None, timeout=60):
    """Returns (status, parsed JSON or text). Doesn't raise on HTTP errors."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw, enc = resp.status, resp.read(), resp.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as e:
        status, raw, enc = e.code, e.read(), e.headers.get("Content-Encoding", "")
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _jwt_payload(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _ms_token_url():
    """Ask GM's sign-in server where its token endpoint is, like onstarjs2 does."""
    try:
        status, body = _http(MS_DISCOVERY_URL, headers={"Accept": "application/json",
                                                        "User-Agent": MOBILE_UA}, timeout=30)
        if status == 200 and isinstance(body, dict) and body.get("token_endpoint"):
            return body["token_endpoint"]
    except OSError:
        pass
    return MS_TOKEN_URL


def _ms_token():
    tokens = _load("microsoft_tokens.json")
    now = int(time.time())
    if tokens.get("expires_at", 0) > now + SKEW:
        return tokens["access_token"]
    if not tokens.get("refresh_token"):
        raise ReauthNeeded("no refresh token stored")
    status, body = _http(_ms_token_url(), data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": MS_CLIENT_ID,
    }, headers={"Accept": "application/json", "User-Agent": MOBILE_UA,
                "Content-Type": "application/x-www-form-urlencoded"})
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        detail = body.get("error_description") or body.get("error") if isinstance(body, dict) else str(body)[:200]
        if status in (400, 401):
            raise ReauthNeeded(f"Microsoft refresh rejected ({status}): {detail}")
        raise RuntimeError(f"Microsoft refresh failed ({status}): {detail}")
    now = int(time.time())
    new = {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", tokens["refresh_token"]),
        "id_token": body.get("id_token", tokens.get("id_token")),
        "expires_in": int(body.get("expires_in", 3600)),
        "expires_at": now + int(body.get("expires_in", 3600)),
    }
    rt_life = body.get("refresh_token_expires_in")
    if rt_life:
        new.update(refresh_expires_in=int(rt_life), refresh_expires_at=now + int(rt_life),
                   refresh_obtained_at=now)
    _save("microsoft_tokens.json", new)
    return new["access_token"]


def _gm_token(vin, device_id):
    try:
        tokens = _load("gm_tokens.json")
        if tokens.get("expires_at", 0) > int(time.time()) + SKEW:
            return tokens["access_token"]
    except (FileNotFoundError, ValueError):
        pass
    status, body = _http(GM_TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": _ms_token(),
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "scope": "onstar gmoc user_trailer user msso priv",
        "device_id": device_id,
    }, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise RuntimeError(f"GM token exchange failed ({status}): {str(body)[:200]}")
    vins = [v.get("vin", "").upper() for v in _jwt_payload(body["access_token"]).get("vehs", [])]
    if vin not in vins:
        raise ReauthNeeded(f"GM token doesn't cover VIN {vin} (has {vins or 'none'})")
    body["expires_in"] = int(body.get("expires_in", 1800))
    body["expires_at"] = int(time.time()) + body["expires_in"]
    _save("gm_tokens.json", body)
    return body["access_token"]


def read_fuel_percent():
    """Fuel level 0-100 as a float. Raises on any failure (ReauthNeeded if tokens are dead)."""
    vin, device_id = _config()
    status, body = _http(HEALTH_URL.format(vin=vin), headers={
        "Authorization": f"Bearer {_gm_token(vin, device_id)}",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US",
        "Content-Type": "application/json",
        "appversion": "myOwner-chevrolet-android-8.5.0-0",
        "locale": "en-US",
        "User-Agent": "myOwner App",
    }, timeout=120)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"health status failed ({status}): {str(body)[:200]}")
    for category in body.get("diagnostics", []):
        for element in category.get("diagnosticElements", []):
            if element.get("name") == "FUEL_LEVEL" and element.get("value") not in (None, ""):
                return float(element["value"])
    raise RuntimeError("health status had no fuel level in it")


if __name__ == "__main__":
    try:
        print(f"fuel level {read_fuel_percent():.0f}%")
    except ReauthNeeded as e:
        sys.exit(f"needs new tokens (run gm-token-tool): {e}")
