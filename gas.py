"""Daily gas-buying call for a Michigan driver: FILL NOW, WAIT, or HALF TANK.

    python gas.py run              fetch prices, store them, make the call, notify, rebuild the page
    python gas.py run --no-notify  same, but don't send the push notification
    python gas.py run --scheduled  for the scheduler: the 7am call, then one quiet midday re-check
    python gas.py test-notify      send a test push to your phone
    python gas.py page             rebuild docs/index.html from stored data

Standard library only. Settings live in config.json; secrets (EIA_API_KEY, NTFY_TOPIC and the
optional TRUCK_STATUS_URL / TRUCK_API_TOKEN) come from environment variables or a local .env file.
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
NOTIFY_HOUR = 7   # Eastern
MIDDAY_HOUR = 12  # Eastern


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


def retail_area(cfg):
    """The AAA area retail prices are tracked for: a metro if set, else the whole state."""
    return cfg.get("metro") or cfg["state"]


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

def open_db(cfg):
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wholesale (date TEXT PRIMARY KEY, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rbob (date TEXT PRIMARY KEY, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS retail_prices (
            date TEXT NOT NULL, area TEXT NOT NULL, price REAL NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (date, area));
        CREATE TABLE IF NOT EXISTS calls (
            date TEXT PRIMARY KEY, call TEXT NOT NULL, reason TEXT NOT NULL,
            retail REAL, wholesale REAL, wholesale_date TEXT, fair REAL, notified_at TEXT);
    """)
    for column in ("margin REAL", "area TEXT", "midday_at TEXT", "fuel_percent REAL"):
        try:
            db.execute(f"ALTER TABLE calls ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass  # column already there
    # The first version kept only statewide prices, in a table called `retail`.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'retail'").fetchone():
        db.execute("INSERT OR IGNORE INTO retail_prices SELECT date, ?, price, source FROM retail",
                   (cfg["state"],))
        db.execute("DROP TABLE retail")
        db.execute("UPDATE calls SET area = ? WHERE area IS NULL", (cfg["state"],))
    db.commit()
    return db


def load_prices(db, table):
    return {date.fromisoformat(d): p for d, p in db.execute(f"SELECT date, price FROM {table}")}


def load_retail(db, cfg):
    rows = db.execute("SELECT date, price FROM retail_prices WHERE area = ?", (retail_area(cfg),))
    return {date.fromisoformat(d): p for d, p in rows}


# ---------- data sources ----------

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_wholesale(cfg, api_key, days=90):
    """Daily NY Harbor gasoline spot prices from the EIA API, as {date: $/gal}."""
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


def fetch_rbob():
    """Latest NY Harbor RBOB spot close from EIA's daily prices page, as {date: $/gal}.

    Fresher than the API series (previous trading day rather than up to a week old), but
    only one day at a time, so history builds up run by run. The page also gives the
    percent change, which recovers the trading day before.
    """
    html = http_get("https://www.eia.gov/todayinenergy/prices.php")
    when = re.search(r"Wholesale Spot Petroleum Prices,\s*(\d{1,2})/(\d{1,2})/(\d{2})\s*Close", html)
    row = re.search(r"Gasoline \(RBOB\).*?NY Harbor</td>\s*<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([+-]?[\d.]+)</td>",
                    html, re.S)
    if not when or not row:
        raise RuntimeError("Couldn't read the RBOB price from EIA's daily prices page")
    day = date(2000 + int(when.group(3)), int(when.group(1)), int(when.group(2)))
    price = float(row.group(1))
    previous = day - timedelta(days=3 if day.weekday() == 0 else 1)
    return {day: price, previous: round(price / (1 + float(row.group(2)) / 100), 3)}


def fetch_retail(cfg):
    """Average regular price from AAA's public state page, for the metro or the whole state.

    Returns (as_of_date, price, backfill) where backfill holds AAA's own
    "yesterday" and "week ago" figures, used to seed history on first runs.
    """
    html = http_get(f"https://gasprices.aaa.com/?state={cfg['state']}")
    m = re.search(r"Price as of\s*(?:<br\s*/?>)?\s*(\d{1,2})/(\d{1,2})/(\d{2})", html)
    if not m:
        raise RuntimeError("Couldn't read the 'Price as of' date from the AAA page")
    as_of = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))

    # The statewide table comes first; each metro has its own table under an <h3>.
    section = html
    if cfg.get("metro"):
        start = html.find(f">{cfg['metro']}</h3>")
        if start < 0:
            metros = ", ".join(re.findall(r"data-title>([^<]+)</h3>", html))
            raise RuntimeError(f"Metro '{cfg['metro']}' isn't on the AAA page. Available: {metros}")
        section = html[start:]

    def regular(label):
        m = re.search(rf"<td>{re.escape(label)}</td>\s*<td>\$([\d.]+)</td>", section)
        if not m or not 1 < float(m.group(1)) < 10:
            raise RuntimeError(f"Couldn't read '{label}' from the AAA page (layout may have changed)")
        return float(m.group(1))

    backfill = {
        as_of - timedelta(days=1): regular("Yesterday Avg."),
        as_of - timedelta(days=7): regular("Week Ago Avg."),
    }
    return as_of, regular("Current Avg."), backfill


def fetch_fuel_percent():
    """Truck's fuel level (0-100) from the GM Truck Remote backend, or None if not set up."""
    url, token = os.environ.get("TRUCK_STATUS_URL"), os.environ.get("TRUCK_API_TOKEN")
    if not url or not token:
        return None
    body = json.loads(http_get(url, {"Authorization": f"Bearer {token}"}, timeout=90))
    for category in body["result"]["diagnostics"]:
        for element in category.get("diagnosticElements", []):
            if element.get("name") == "FUEL_LEVEL" and element.get("value") is not None:
                return float(element["value"])
    raise RuntimeError("Truck status had no fuel level in it")


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


def wholesale_rise(series, cfg, today):
    """(rise, last_day) over the last N trading days of `series`, or None if it can't be judged."""
    days = sorted(d for d in series if d <= today)
    n = cfg["wholesale_jump_days"]
    if len(days) <= n or (today - days[-1]).days > cfg["wholesale_max_age_days"]:
        return None
    if (days[-1] - days[-1 - n]).days > n + 4:  # gaps in the series; not really N trading days
        return None
    return series[days[-1]] - series[days[-1 - n]], days[-1]


def decide(retail, wholesale, cfg, today, rbob=None):
    """Apply the rules in priority order.

    retail / wholesale / rbob are {date: price}; retail must contain `today`.
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

    # 3. Wholesale is climbing -> retail will follow. Prefer the fresher RBOB series
    #    once it has enough days; until then use the EIA API series.
    rising = wholesale_rise(rbob or {}, cfg, today) or wholesale_rise(wholesale, cfg, today)
    if rising and rising[0] >= cfg["wholesale_jump"]:
        return FILL, (f"Wholesale gas rose {cents(rising[0])} in {cfg['wholesale_jump_days']} trading days "
                      f"(through {rising[1]:%b %d}); retail will likely follow.")

    # 4. Nothing decisive.
    reason = (f"No strong signal: retail ${price:.2f} is {cents(gap)} "
              f"{'above' if gap >= 0 else 'below'} fair (${fair:.2f}).")
    if len(window) < fall_days - 1:
        reason += f" (Only {len(window)} days of retail history so far.)"
    return HALF, reason


def tank_advice(call, reason, fuel, cfg):
    """Fit the message to how full the truck is. Returns (reason, quiet).

    `fuel` is percent full, or None when the truck isn't connected.
    `quiet` means the notification isn't worth a buzz (the tank is already full).
    """
    tank = cfg["tank_gallons"]
    if fuel is None:
        return (f"{reason} Buy about {tank / 2:.0f} gal." if call == HALF else reason), False
    room = tank * (100 - fuel) / 100
    if fuel >= cfg["tank_full_percent"]:
        return f"Tank is {fuel:.0f}% full, so no need to stop. {reason}", True
    if call == WAIT:
        if fuel <= cfg["tank_low_percent"]:
            return f"{reason} Tank is at {fuel:.0f}%, so buy just enough to get by.", False
        return f"{reason} Tank is at {fuel:.0f}%.", False
    if call == FILL:
        return f"{reason} Tank is at {fuel:.0f}%: about {room:.0f} gal to fill.", False
    return f"{reason} Tank is at {fuel:.0f}%: buy about {min(tank / 2, room):.0f} gal.", False


# ---------- scorecard ----------

def score_calls(db, retail, cfg):
    """Judge past calls by what retail did over the following `score_days` days.

    WAIT was right if the price dropped; FILL NOW was right if it rose; HALF TANK isn't
    judged, just measured. Returns ({date: outcome}, summary).
    """
    horizon, threshold = cfg["score_days"], cfg["score_threshold"]
    latest = max(retail, default=None)
    outcomes = {}
    tally = {WAIT: [0, 0], FILL: [0, 0]}
    half_moves = []
    rows = db.execute("SELECT date, call, retail FROM calls WHERE area = ? AND retail IS NOT NULL",
                      (retail_area(cfg),))
    for day, call, price in rows:
        day = date.fromisoformat(day)
        later = [retail[day + timedelta(days=i)] for i in range(1, horizon + 1)
                 if day + timedelta(days=i) in retail]
        if latest is None or day + timedelta(days=horizon) > latest or len(later) < 3:
            continue  # too soon (or too many missing days) to judge
        if call == WAIT:
            move, right = min(later) - price, min(later) - price <= -threshold
            text = f"Fell {cents(move)} within {horizon} days" if right else f"Didn't fall within {horizon} days"
        elif call == FILL:
            move, right = max(later) - price, max(later) - price >= threshold
            text = f"Rose {cents(move)} within {horizon} days" if right else f"Didn't rise within {horizon} days"
        else:
            move, right = later[-1] - price, None
            text = f"{'Up' if move > 0 else 'Down'} {cents(move)} after {horizon} days"
            half_moves.append(move)
        if right is not None:
            tally[call][0] += right
            tally[call][1] += 1
        outcomes[day] = {"text": text, "right": right}
    summary = {
        "days": horizon,
        "wait": tally[WAIT], "fill": tally[FILL],
        "half": [len(half_moves), sum(half_moves) / len(half_moves) if half_moves else None],
    }
    return outcomes, summary


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
    retail = load_retail(db, cfg)
    wholesale = load_prices(db, "wholesale")
    calls = {date.fromisoformat(r[0]): r[1:] for r in
             db.execute("SELECT date, call, reason, fair, fuel_percent FROM calls")}
    margin, rolling = current_margin(retail, wholesale, cfg, max(retail, default=today))
    outcomes, scorecard = score_calls(db, retail, cfg)
    rows = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        call, reason, stored_fair, _ = calls.get(d, (None, None, None, None))
        # Days with a call keep the fair price that call was made with.
        fair = stored_fair if stored_fair is not None else fair_price(wholesale, d, margin)[0]
        rows.append({"date": d.isoformat(), "retail": retail.get(d), "fair": fair,
                     "call": call, "reason": reason, "outcome": outcomes.get(d)})
    ws_latest = max(wholesale) if wholesale else None
    data = {
        "days": rows,
        "scorecard": scorecard,
        "fuel": calls[max(calls)][3] if calls else None,
        "area": cfg.get("area_label") or retail_area(cfg),
        "margin": margin,
        "margin_note": (f"auto-tuned: {cfg['margin_percentile']}th percentile of the retail-wholesale "
                        f"gap over the last {cfg['margin_window_days']} days" if rolling
                        else "fixed" if cfg.get("margin_mode") != "rolling"
                        else f"fixed until there are {cfg['margin_min_days']} days of history, then auto-tuned"),
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
    db = open_db(cfg)
    area = retail_area(cfg)
    earlier = db.execute("SELECT call, notified_at, midday_at, fuel_percent FROM calls WHERE date = ?",
                         (today.isoformat(),)).fetchone()

    # Scheduled runs: the morning call once, then one midday re-check that only
    # speaks up if the call changed to something worth acting on.
    args.midday = False
    if args.scheduled:
        if now.hour < NOTIFY_HOUR:
            print(f"It's {now:%H:%M} Eastern, before {NOTIFY_HOUR}:00 - nothing to do yet.")
            return
        if earlier and earlier[1]:
            if now.hour < MIDDAY_HOUR or earlier[2]:
                print("Already sent today's call; nothing to do right now.")
                return
            args.midday = True

    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        raise RuntimeError("EIA_API_KEY is not set (see README)")

    db.executemany("INSERT OR REPLACE INTO wholesale VALUES (?, ?)",
                   [(d.isoformat(), p) for d, p in fetch_wholesale(cfg, api_key).items()])
    try:
        rbob = fetch_rbob()
        for d, p in rbob.items():
            # The derived previous day never overwrites a value read directly.
            verb = "REPLACE" if d == max(rbob) else "IGNORE"
            db.execute(f"INSERT OR {verb} INTO rbob VALUES (?, ?)", (d.isoformat(), p))
    except Exception as e:
        print(f"  (skipping daily RBOB price: {e})")
    as_of, price, backfill = fetch_retail(cfg)
    db.execute("INSERT OR REPLACE INTO retail_prices VALUES (?, ?, ?, 'aaa')",
               (as_of.isoformat(), area, price))
    db.executemany("INSERT OR IGNORE INTO retail_prices VALUES (?, ?, ?, 'aaa-backfill')",
                   [(d.isoformat(), area, p) for d, p in backfill.items()])
    db.commit()

    # Ask the truck once a day (it wakes the truck's modem); midday reuses the morning reading.
    fuel = earlier[3] if earlier and args.midday else None
    if fuel is None:
        try:
            fuel = fetch_fuel_percent()
        except Exception as e:
            print(f"  (couldn't read the truck's fuel level: {e})")

    retail = load_retail(db, cfg)
    wholesale = load_prices(db, "wholesale")
    call, reason = decide(retail, wholesale, cfg, as_of, load_prices(db, "rbob"))
    reason, quiet = tank_advice(call, reason, fuel, cfg)
    margin, rolling = current_margin(retail, wholesale, cfg, as_of)
    fair, ws_date = fair_price(wholesale, as_of, margin)
    db.execute(
        "INSERT INTO calls (date, call, reason, retail, wholesale, wholesale_date, fair, margin, "
        "area, fuel_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET call=excluded.call, reason=excluded.reason, "
        "retail=excluded.retail, wholesale=excluded.wholesale, "
        "wholesale_date=excluded.wholesale_date, fair=excluded.fair, margin=excluded.margin, "
        "area=excluded.area, fuel_percent=excluded.fuel_percent",
        (today.isoformat(), call, reason, price, wholesale[ws_date], ws_date.isoformat(), fair,
         margin, area, fuel))
    if args.midday:
        db.execute("UPDATE calls SET midday_at = ? WHERE date = ?",
                   (now.isoformat(timespec="seconds"), today.isoformat()))
    db.commit()
    build_page(db, cfg, today)

    print(f"{call}: {reason}")
    print(f"  retail ${price:.3f} (AAA {area}, as of {as_of})  wholesale ${wholesale[ws_date]:.3f} "
          f"(EIA, {ws_date})  fair ${fair:.3f}  "
          f"margin ${margin:.3f} ({'rolling' if rolling else 'fixed'})"
          + (f"  tank {fuel:.0f}%" if fuel is not None else ""))

    tags, priority = NTFY_STYLE[call]
    if quiet:
        priority = "2"
    if args.midday:
        if call != earlier[0] and call != HALF and not quiet:
            notify(cfg, f"Update: {call}", f"Changed from {earlier[0]} this morning. {reason}", tags, priority)
            print("  midday update sent")
        else:
            print("  midday check: nothing new worth a notification")
    elif not args.no_notify:
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
                       help="morning call at/after 7am Eastern, then one quiet midday re-check")
    sub.add_parser("test-notify", help="send a test push notification")
    sub.add_parser("page", help="rebuild the web page from stored data")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles choke on ¢
    load_env()
    if args.command == "test-notify":
        notify(load_config(), "Gas helper test", "If you can read this, notifications work.")
        print("Test notification sent.")
    elif args.command == "page":
        cfg = load_config()
        build_page(open_db(cfg), cfg, eastern_now().date())
        print(f"Wrote {PAGE_OUT}")
    else:
        try:
            run(args)
        except Exception as e:
            # Make a silent morning distinguishable from a broken one.
            if not args.no_notify and not getattr(args, "midday", False) and os.environ.get("NTFY_TOPIC"):
                try:
                    notify(load_config(), "Gas helper: no call today", str(e), "warning", "2")
                except Exception:
                    pass
            sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
