# Gas helper

Every morning around 7am Eastern it sends one push notification: **FILL NOW**, **WAIT**, or
**HALF TANK**, with a one-line reason. It also keeps a history in `data/gas.db` (SQLite) and
rebuilds a web page (`docs/index.html`) showing the last 30 days.

## Setup

### 1. EIA API key (free)

1. Go to <https://www.eia.gov/opendata/register.php>.
2. Enter your name and email, accept the terms, and submit.
3. EIA emails you a key (a ~40-character string) within a minute or two.
4. Open `.env` in this folder and paste it after `EIA_API_KEY=`.

### 2. ntfy on your phone (free, no account)

1. Install **ntfy** from the App Store or Google Play.
2. Open `.env` and find the `NTFY_TOPIC=` line. A random topic name is already there.
   Anyone who knows the topic name can read and send to it, so treat it like a password.
3. In the ntfy app tap **+** (Subscribe to topic), type that exact topic name, leave
   the server as `ntfy.sh`, and subscribe.
4. Allow notifications when the phone asks. On Android, also turn off battery optimization
   for ntfy if pushes arrive late. On iPhone, nothing else is needed.
5. Test it from this folder:

   ```bash
   python gas.py test-notify
   ```

### 3. Try it once

```bash
python gas.py run
```

That fetches prices, stores them, prints the call, sends the push, and rebuilds
`docs/index.html` (open it in a browser). Add `--no-notify` to skip the push.

### 4. How it runs every day

The whole daily job runs on the Mac Mini, from `mac/daily.py`, at 7:00am and noon Eastern:

1. pull the latest code and history from GitHub,
2. at 7:00, read the truck's fuel level from the GM Truck Remote backend on the same machine,
3. run `gas.py` (fetch prices, make the call, send the notification, rebuild the page),
4. push the updated history and page back to GitHub.

GitHub only hosts the page (**Settings → Pages → `main` / `/docs`**) and runs the on-demand
job behind the app's buttons (`.github/workflows/daily.yml`). If the Mac is off at 7am there
is no call that morning.

On the Mac everything is in `~/SERVER FILES/gas-fuel/`: `repo/` is a clone of this repo
(with its own `.env` holding `EIA_API_KEY` and `NTFY_TOPIC`), `deploy_key` can write to this
repo only, and `fuel.log` is the job's log. The schedule is
`~/Library/LaunchAgents/com.gasfuel.plist` (a copy of `mac/com.gasfuel.plist`). Because the
job pulls before it runs, pushing a change to this repo is all it takes to update the Mac.

To run it by hand on the Mac: `python3 "$HOME/SERVER FILES/gas-fuel/repo/mac/daily.py" --no-notify`

### 5. Optional: buttons in the app that do things

The page's **Refresh** button only reloads what's already published. **Get new prices**, and
adding or removing a road trip, start the GitHub job on demand (without sending a
notification) and reload when it finishes. They need a GitHub token, pasted into the app once
and stored only on that device:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Name it (e.g. "Gas Call app") and pick an expiration.
3. **Repository access → Only select repositories →** this repo.
4. **Permissions → Repository permissions → Actions → Read and write.** Nothing else.
5. Generate, copy the token, open the app, tap **Get new prices**, paste, **Save**.

If that token ever leaked, the most someone could do is start or cancel this repo's job.
Delete it at <https://github.com/settings/personal-access-tokens> to revoke it.

## Road trips

The **Trips** tab plans gas stops for a drive. Add a trip (destination and date) from the app,
or with `python gas.py trip-add "Houghton, MI" 2026-11-01`. The helper then:

- finds the driving route from `home` and the counties it passes through, in order;
- prices each county with AAA's daily county average, and shows the week's change;
- marks the cheapest and priciest areas and works out where to buy gas: the cheapest plan
  that never drops below `trip_reserve_gallons`, counting `trip_stop_cost` dollars of hassle
  per stop so it won't add a stop to save a few cents;
- sends a notification with the plan the morning before and the morning of the trip.

County averages compare areas, not stations: the cheapest station in a pricey county can
still beat the average of a cheap one. Routes come from OpenStreetMap services (Nominatim for
the place, the OSRM demo server for the route) and county lookups from the FCC's Area API;
all three are only called when a trip is added. Trips are one-way.

## The rules

Checked in this order; the first match wins.

| # | Condition | Call |
|---|-----------|------|
| 1 | Retail is up 15¢+ versus its low of the previous 3 days | WAIT |
| 2 | Retail has fallen for 7+ days and is within 10¢ of fair | FILL NOW |
| 3 | Wholesale is up 10¢+ over 3 trading days | FILL NOW |
| 4 | Otherwise | HALF TANK |

A second, quiet check runs around noon Eastern. It only notifies if the call has changed to
WAIT or FILL NOW since the morning.

The page's **Scorecard** judges each call by what retail did over the next 5 days: WAIT was
right if the price fell 3¢ or more, FILL NOW was right if it rose 3¢ or more.

**Fair retail** = latest wholesale price + margin. The margin tunes itself (see `margin_mode` below).

## Settings (`config.json`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `margin` | 0.65 | Starting margin: dollars added to wholesale for taxes + distribution + station margin. Used until the rolling margin has enough history, or always if `margin_mode` is `fixed` |
| `margin_mode` | rolling | `rolling` tunes the margin to the market; `fixed` always uses `margin` |
| `margin_window_days`, `margin_percentile` | 60, 20 | Rolling margin = this percentile of the retail-minus-wholesale gap over this many days (a low percentile approximates the bottom of the price cycle) |
| `margin_min_days` | 14 | Days of retail history needed before the rolling margin takes over |
| `retail_jump`, `retail_jump_days` | 0.15, 3 | Rule 1 |
| `falling_days`, `near_fair` | 7, 0.10 | Rule 2 |
| `falling_tolerance` | 0.005 | A day-over-day rise up to this (half a cent) doesn't break a "falling" streak |
| `wholesale_jump`, `wholesale_jump_days` | 0.10, 3 | Rule 3 |
| `wholesale_max_age_days` | 8 | Rule 3 is ignored when the newest wholesale price is older than this |
| `tank_gallons` | 24 | Used for the "buy about N gal" hint |
| `tank_full_percent`, `tank_low_percent` | 75, 15 | With the truck connected: above "full" the notification is silent; below "low" a WAIT says to buy just enough to get by |
| `home` | Freeport, MI | Where road trips start (name, latitude, longitude) |
| `trip_mpg` | 18 | Highway miles per gallon, for trip planning |
| `trip_reserve_gallons` | 3 | Gas the trip plan always keeps in the tank |
| `trip_stop_cost` | 2.0 | Dollars of hassle per gas stop; raise it for fewer stops |
| `score_days`, `score_threshold` | 5, 0.03 | Scorecard: days to look ahead, and the move (dollars) that counts as right |
| `state`, `metro` | MI, Grand Rapids-Muskegon-Holland | Retail area. `metro` must match a metro name on AAA's state page; leave it empty for the statewide average |
| `area_label` | Grand Rapids area | Short name for the area on the page |
| `eia_series` | `EER_EPMRU_PF4_Y35NY_DPG` | EIA wholesale series |
| `page_url` | "" | If set, tapping the notification opens it |

## Data sources

- **Wholesale:** EIA Open Data API, *New York Harbor Conventional Gasoline Regular Spot Price*.
  EIA does not publish a NY Harbor RBOB spot series (its RBOB futures series ended in April
  2024); this is the closest free daily series and moves with RBOB. The values are daily, but
  EIA posts them in weekly batches, so the newest one can be up to about a week old.
- **Fresher wholesale (rule 3 only):** the NY Harbor RBOB spot close from EIA's daily prices
  page (`eia.gov/todayinenergy/prices.php`), which is the previous trading day's price. It
  gives one day at a time, so the rule switches to it once a few days have been collected.
  EIA republishes these figures from Refinitiv with permission; this is for personal use.
- **Truck fuel level:** read at 7:00am from the GM Truck Remote backend on the Mac Mini and
  saved as `data/fuel.json` (just the percentage). The truck backend's token never leaves the
  Mac. Readings older than 24 hours are ignored.
- **County prices:** AAA's county averages for Michigan (and any other state on an upcoming
  trip), from the map data on the same state page.
- **Retail:** AAA's public state page (`gasprices.aaa.com/?state=MI`), read a few times a day
  (morning call, midday check, manual refreshes). It includes the metro averages.
  The site posts no terms of use and its `robots.txt` allows crawling (with a 10-second
  delay). The page says "All Rights Reserved", so keep this to personal use and don't
  republish the numbers. EIA has no Michigan retail series.

## Files

- `gas.py` — fetch, store, rules, notify, page build
- `trips.py` — road trips: route, county prices, fill-up plan
- `mac/` — the daily job and its schedule, as installed on the Mac Mini
- `config.json` — settings
- `page_template.html` — web page template; output goes to `docs/index.html`
- `test_rules.py` — rule tests (`python -m unittest`)
- `.github/workflows/daily.yml` — the on-demand job behind the app's buttons
