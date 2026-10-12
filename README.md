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

### 4. Run it every morning with GitHub Actions

Runs on GitHub's servers, so your computer can be off.

1. Create a new repository at <https://github.com/new> (no README or .gitignore).
   Public is fine: your key and topic are stored as secrets, never in the code. A public repo
   also lets you use GitHub Pages for free.
2. Push this folder to it:

   ```bash
   git add -A
   git commit -m "Gas helper"
   git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
   git push -u origin main
   ```

3. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Add two secrets with the same values as in `.env`: `EIA_API_KEY` and `NTFY_TOPIC`.
4. **Actions** tab → **Daily gas call** → **Run workflow** to test it. You should get a push.
5. For the web page: **Settings → Pages → Deploy from a branch → `main` / `/docs`**.
   The page appears at `https://YOUR-USERNAME.github.io/YOUR-REPO/`. Put that URL in
   `page_url` in `config.json` and tapping the notification will open it.

GitHub's scheduler often starts a few minutes late, so expect the push between about 7:05
and 7:20. Each run commits the updated database and page back to the repo; run `git pull`
before editing settings locally.

**Alternative, if this PC is on every morning:** Windows Task Scheduler → Create Basic Task →
Daily 7:00 AM → Start a program → `python` with arguments `gas.py run` and "Start in" set to
this folder.

### 5. Optional: "Get new prices" button in the app

The page's **Refresh** button only reloads what's already published. **Get new prices** starts
the GitHub job on demand (without sending a notification) and reloads when it finishes. It
needs a GitHub token, pasted into the app once and stored only on that device:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Name it (e.g. "Gas Call app") and pick an expiration.
3. **Repository access → Only select repositories →** this repo.
4. **Permissions → Repository permissions → Actions → Read and write.** Nothing else.
5. Generate, copy the token, open the app, tap **Get new prices**, paste, **Save**.

If that token ever leaked, the most someone could do is start or cancel this repo's job.
Delete it at <https://github.com/settings/personal-access-tokens> to revoke it.

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
- **Truck fuel level (optional):** if `TRUCK_STATUS_URL` and `TRUCK_API_TOKEN` are set, the
  morning run asks the GM Truck Remote backend for the fuel level and fits the message to it.
- **Retail:** AAA's public state page (`gasprices.aaa.com/?state=MI`), read a few times a day
  (morning call, midday check, manual refreshes). It includes the metro averages.
  The site posts no terms of use and its `robots.txt` allows crawling (with a 10-second
  delay). The page says "All Rights Reserved", so keep this to personal use and don't
  republish the numbers. EIA has no Michigan retail series.

## Files

- `gas.py` — everything: fetch, store, rules, notify, page build
- `config.json` — settings
- `page_template.html` — web page template; output goes to `docs/index.html`
- `test_rules.py` — rule tests (`python -m unittest`)
- `.github/workflows/daily.yml` — the daily schedule
