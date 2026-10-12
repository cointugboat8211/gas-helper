"""Daily gas-buying call for a Michigan driver: FILL NOW, WAIT, or HALF TANK.

    python gas.py run              fetch prices, store them, make the call, notify, rebuild the page
    python gas.py run --no-notify  same, but don't send the push notification
    python gas.py run --scheduled  for the scheduler: only acts once per day, at/after 7am Eastern
    python gas.py test-notify      send a test push to your phone
    python gas.py page             rebuild docs/index.html from stored data

Standard library only. Settings live in config.json; secrets (EIA_API_KEY, NTFY_TOPIC)
come from environment variables or a local .env file.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "gas.db"
PAGE_TEMPLATE = ROOT / "page_template.html"
PAGE_OUT = ROOT / "docs" / "index.html"
USER_AGENT = "gas-helper/1.0 (personal use; one request per day)"

FILL, WAIT, HALF = "FILL NOW", "WAIT", "HALF TANK"
NOTIFY_HOUR = 7  # Eastern


# ---------- settings ----------

def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def eastern_now():
    """Current US Eastern wall-clock time (no tz database needed, which Windows lacks)."""
    utc = datetime.now(timezone.utc)

    def nth_sunday(month, n):
        first = date(utc.year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)

    dst_start = datetime.combine(nth_sunday(3, 2), time(7), tzinfo=timezone.utc)   # 2am EST
    dst_end = datetime.combine(nth_sunday(11, 1), time(6), tzinfo=timezone.utc)    # 2am EDT
    offset = -4 if dst_start <= utc < dst_end else -5
    return (utc + timedelta(hours=offset)).replace(tzinfo=None)


# ---------- storage ----------

def open_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wholesale (date TEXT PRIMARY KEY, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS retail (
            date TEXT PRIMARY KEY, price REAL NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls (
            date TEXT PRIMARY KEY, call TEXT NOT NULL, reason TEXT NOT NULL,
            retail REAL, wholesale REAL, wholesale_date TEXT, fair REAL, notified_at TEXT);
    """)
    try:
        db.execute("ALTER TABLE calls ADD COLUMN margin REAL")
    except sqlite3.OperationalError:
        pass  # column already there
    return db


def load_prices(db, table):
    return {date.fromisoformat(d): p for d, p in db.execute(f"SELECT date, price FROM {table}")}


# ---------- data sources ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_wholesale(cfg, api_key, days=90):
    """Daily NY Harbor gasoline spot prices from EIA, as {date: $/gal}."""
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": cfg["eia_series"],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": days,
    })
    try:
        body = http_get("https://api.eia.gov/v2/petroleum/pri/spt/data/?" + params)
    except urllib.error.HTTPError as e:
        # Don't let the URL (which contains the key) leak into logs.
        raise RuntimeError(f"EIA API returned HTTP {e.code} (check EIA_API_KEY)") from None
    rows = json.loads(body)["response"]["data"]
    prices = {date.fromisoformat(r["period"]): float(r["value"]) for r in rows if r["value"] is not None}
    if not prices:
        raise RuntimeError(f"EIA returned no data for series {cfg['eia_series']}")
    return prices


def fetch_retail(cfg):
    """State average regular price from AAA's public page.

    Returns (as_of_date, price, backfill) where backfill holds AAA's own
    "yesterday" and "week ago" figures, used to seed history on first runs.
    """
    html = http_get(f"https://gasprices.aaa.com/?state={cfg['state']}")

    def regular(label):
        # The first table on the page is the statewide one; metro tables follow.
        m = re.search(rf"<td>{re.escape(label)}</td>\s*<td>\$([\d.]+)</td>", html)
        if not m or not 1 < float(m.group(1)) < 10:
            raise RuntimeError(f"Couldn't read '{label}' from the AAA page (layout may have changed)")
        return float(m.group(1))

    m = re.search(r"Price as of\s*(?:<br\s*/?>)?\s*(\d{1,2})/(\d{1,2})/(\d{2})", html)
    if not m:
        raise RuntimeError("Couldn't read the 'Price as of' date from the AAA page")
    as_of = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
    backfill = {
        as_of - timedelta(days=1): regular("Yesterday Avg."),
        as_of - timedelta(days=7): regular("Week Ago Avg."),
    }
    return as_of, regular("Current Avg."), backfill


# ---------- the rules ----------

def cents(dollars):
    return f"{abs(dollars) * 100:.0f}¢"


def fair_price(wholesale, day, margin):
    """Fair retail on `day`: latest wholesale price on or before it, plus margin."""
    known = [d for d in wholesale if d <= day]
    if not known:
        return None, None
    latest = max(known)
    return wholesale[latest] + margin, latest


def current_margin(retail, wholesale, cfg, today):
    """Margin to add to wholesale. Returns (dollars, is_rolling).

    In rolling mode it tunes itself: a low percentile of the retail-minus-wholesale gap
    over the last `margin_window_days`, i.e. roughly where the gap bottoms out before
    a spike. Until there are `margin_min_days` of history it uses the fixed `margin`.
    """
    if cfg.get("margin_mode") != "rolling":
        return cfg["margin"], False
    start = today - timedelta(days=cfg["margin_window_days"])
    gaps = sorted(p - fair_price(wholesale, d, 0)[0] for d, p in retail.items()
                  if start <= d <= today and fair_price(wholesale, d, 0)[0] is not None)
    if len(gaps) < cfg["margin_min_days"]:
        return cfg["margin"], False
    pos = (len(gaps) - 1) * cfg["margin_percentile"] / 100
    lo = int(pos)
    hi = min(lo + 1, len(gaps) - 1)
    return gaps[lo] + (gaps[hi] - gaps[lo]) * (pos - lo), True


def decide(retail, wholesale, cfg, today):
    """Apply the rules in priority order.

    retail / wholesale are {date: price}; retail must contain `today`.
    Returns (call, one-line reason).
    """
    price = retail[today]
    margin, _ = current_margin(retail, wholesale, cfg, today)
    fair, _ = fair_price(wholesale, today, margin)
    gap = price - fair

    # 1. Retail just spiked -> wait for the Michigan price cycle to drift back down.
    jump_days = cfg["retail_jump_days"]
    recent = [p for d, p in retail.items() if today - timedelta(days=jump_days) <= d < today]
    if recent and price - min(recent) >= cfg["retail_jump"]:
        return WAIT, (f"Retail jumped {cents(price - min(recent))} in {jump_days} days to ${price:.2f}; "
                      "prices usually drift back down after a spike.")

    # 2. Long slide that has reached fair value -> a spike is likely next.
    fall_days = cfg["falling_days"]
    window = sorted((d, p) for d, p in retail.items() if today - timedelta(days=fall_days) <= d <= today)
    steps = [b[1] - a[1] for a, b in zip(window, window[1:])]
    falling = (
        len(window) >= fall_days - 1                      # allow a couple of missed days
        and (today - window[0][0]).days >= fall_days - 1
        and all(s <= cfg["falling_tolerance"] for s in steps)
        and price < window[0][1]
    )
    if falling and gap <= cfg["near_fair"]:
        where = f"{cents(gap)} {'above' if gap >= 0 else 'below'} fair"
        return FILL, (f"Retail has fallen {fall_days}+ days to ${price:.2f}, only {where} "
                      f"(${fair:.2f}); a spike is likely soon.")

    # 3. Wholesale is climbing -> retail will follow.
    ws_days = sorted(d for d in wholesale if d <= today)
    n = cfg["wholesale_jump_days"]
    if len(ws_days) > n and (today - ws_days[-1]).days <= cfg["wholesale_max_age_days"]:
        rise = wholesale[ws_days[-1]] - wholesale[ws_days[-1 - n]]
        if rise >= cfg["wholesale_jump"]:
            return FILL, (f"Wholesale gas rose {cents(rise)} in {n} trading days "
                          f"(through {ws_days[-1]:%b %d}); retail will likely follow.")

    # 4. Nothing decisive.
    half = cfg["tank_gallons"] / 2
    reason = (f"No strong signal: retail ${price:.2f} is {cents(gap)} "
              f"{'above' if gap >= 0 else 'below'} fair (${fair:.2f}). Buy about {half:.0f} gal.")
    if len(window) < fall_days - 1:
        reason += f" (Only {len(window)} days of retail history so far.)"
    return HALF, reason


# ---------- notification ----------

NTFY_STYLE = {FILL: ("fuelpump", "4"), WAIT: ("hourglass_flowing_sand", "3"), HALF: ("fuelpump", "3")}


def notify(cfg, title, message, tags="fuelpump", priority="3"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not set (see README)")
    headers = {"Title": title, "Tags": tags, "Priority": priority}
    if cfg.get("page_url"):
        headers["Click"] = cfg["page_url"]
    req = urllib.request.Request(f"{cfg['ntfy_server'].rstrip('/')}/{topic}",
                                 data=message.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=30).close()


# ---------- web page ----------

def build_page(db, cfg, today, days=30):
    retail = load_prices(db, "retail")
    wholesale = load_prices(db, "wholesale")
    calls = {date.fromisoformat(r[0]): r[1:] for r in
             db.execute("SELECT date, call, reason, fair FROM calls")}
    margin, rolling = current_margin(retail, wholesale, cfg, max(retail, default=today))
    rows = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        call, reason, stored_fair = calls.get(d, (None, None, None))
        # Days with a call keep the fair price that call was made with.
        fair = stored_fair if stored_fair is not None else fair_price(wholesale, d, margin)[0]
        rows.append({"date": d.isoformat(), "retail": retail.get(d), "fair": fair,
                     "call": call, "reason": reason})
    ws_latest = max(wholesale) if wholesale else None
    data = {
        "days": rows,
        "margin": margin,
        "margin_note": (f"auto-tuned: {cfg['margin_percentile']}th percentile of the retail-wholesale "
                        f"gap over the last {cfg['margin_window_days']} days" if rolling
                        else "fixed" if cfg.get("margin_mode") != "rolling"
                        else f"fixed until there are {cfg['margin_min_days']} days of history, then auto-tuned"),
        "state": cfg["state"],
        "wholesale": wholesale.get(ws_latest),
        "wholesale_date": ws_latest.isoformat() if ws_latest else None,
        "generated": eastern_now().strftime("%b %d, %Y %I:%M %p ET"),
        "stamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": cfg.get("github_repo", ""),
    }
    blob = json.dumps(data).replace("</", "<\\/")
    PAGE_OUT.parent.mkdir(exist_ok=True)
    PAGE_OUT.write_text(PAGE_TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", blob),
                        encoding="utf-8")


# ---------- commands ----------

def run(args):
    cfg = load_config()
    now = eastern_now()
    today = now.date()
    db = open_db()

    if args.scheduled:
        if now.hour < NOTIFY_HOUR:
            print(f"It's {now:%H:%M} Eastern, before {NOTIFY_HOUR}:00 - nothing to do yet.")
            return
        if db.execute("SELECT 1 FROM calls WHERE date = ? AND notified_at IS NOT NULL",
                      (today.isoformat(),)).fetchone():
            print("Already sent today's call.")
            return

    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        raise RuntimeError("EIA_API_KEY is not set (see README)")

    db.executemany("INSERT OR REPLACE INTO wholesale VALUES (?, ?)",
                   [(d.isoformat(), p) for d, p in fetch_wholesale(cfg, api_key).items()])
    as_of, price, backfill = fetch_retail(cfg)
    db.execute("INSERT OR REPLACE INTO retail VALUES (?, ?, 'aaa')", (as_of.isoformat(), price))
    db.executemany("INSERT OR IGNORE INTO retail VALUES (?, ?, 'aaa-backfill')",
                   [(d.isoformat(), p) for d, p in backfill.items()])
    db.commit()

    retail = load_prices(db, "retail")
    wholesale = load_prices(db, "wholesale")
    call, reason = decide(retail, wholesale, cfg, as_of)
    margin, rolling = current_margin(retail, wholesale, cfg, as_of)
    fair, ws_date = fair_price(wholesale, as_of, margin)
    db.execute(
        "INSERT INTO calls (date, call, reason, retail, wholesale, wholesale_date, fair, margin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET call=excluded.call, reason=excluded.reason, "
        "retail=excluded.retail, wholesale=excluded.wholesale, "
        "wholesale_date=excluded.wholesale_date, fair=excluded.fair, margin=excluded.margin",
        (today.isoformat(), call, reason, price, wholesale[ws_date], ws_date.isoformat(), fair, margin))
    db.commit()
    build_page(db, cfg, today)

    print(f"{call}: {reason}")
    print(f"  retail ${price:.3f} (AAA, as of {as_of})  wholesale ${wholesale[ws_date]:.3f} "
          f"(EIA, {ws_date})  fair ${fair:.3f}  "
          f"margin ${margin:.3f} ({'rolling' if rolling else 'fixed'})")

    if not args.no_notify:
        tags, priority = NTFY_STYLE[call]
        notify(cfg, call, reason, tags, priority)
        db.execute("UPDATE calls SET notified_at = ? WHERE date = ?",
                   (now.isoformat(timespec="seconds"), today.isoformat()))
        db.commit()
        print("  notification sent")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="fetch, decide, notify, rebuild page")
    p_run.add_argument("--no-notify", action="store_true")
    p_run.add_argument("--scheduled", action="store_true",
                       help="only act once per day, at/after 7am Eastern")
    sub.add_parser("test-notify", help="send a test push notification")
    sub.add_parser("page", help="rebuild the web page from stored data")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles choke on ¢
    load_env()
    if args.command == "test-notify":
        notify(load_config(), "Gas helper test", "If you can read this, notifications work.")
        print("Test notification sent.")
    elif args.command == "page":
        build_page(open_db(), load_config(), eastern_now().date())
        print(f"Wrote {PAGE_OUT}")
    else:
        try:
            run(args)
        except Exception as e:
            # Make a silent morning distinguishable from a broken one.
            if not args.no_notify and os.environ.get("NTFY_TOPIC"):
                try:
                    notify(load_config(), "Gas helper: no call today", str(e), "warning", "2")
                except Exception:
                    pass
            sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
