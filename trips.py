"""Road trips: the counties along a route, their average gas prices, and where to fill up.

Prices are AAA county averages (the same public state page the daily call uses), so the
advice is "this area is cheaper than that one", not a specific station.
"""
import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRIPS_PATH = ROOT / "data" / "trips.json"
USER_AGENT = "gas-helper/1.0 (personal use; github.com/cointugboat8211/gas-helper)"
AAA_CRAWL_DELAY = 10  # seconds between AAA requests, as its robots.txt asks
SAMPLE_MILES = 8      # how often to check which county the route is in

# Best-known town in each Michigan county, so a county reads as a place you'd stop.
MI_TOWNS = {
    "Alcona": "Harrisville", "Alger": "Munising", "Allegan": "Allegan", "Alpena": "Alpena",
    "Antrim": "Bellaire", "Arenac": "Standish", "Baraga": "L'Anse", "Barry": "Hastings",
    "Bay": "Bay City", "Benzie": "Beulah", "Berrien": "St. Joseph", "Branch": "Coldwater",
    "Calhoun": "Battle Creek", "Cass": "Cassopolis", "Charlevoix": "Charlevoix",
    "Cheboygan": "Cheboygan", "Chippewa": "Sault Ste. Marie", "Clare": "Clare",
    "Clinton": "St. Johns", "Crawford": "Grayling", "Delta": "Escanaba",
    "Dickinson": "Iron Mountain", "Eaton": "Charlotte", "Emmet": "Petoskey", "Genesee": "Flint",
    "Gladwin": "Gladwin", "Gogebic": "Ironwood", "Grand Traverse": "Traverse City",
    "Gratiot": "Alma", "Hillsdale": "Hillsdale", "Houghton": "Houghton", "Huron": "Bad Axe",
    "Ingham": "Lansing", "Ionia": "Ionia", "Iosco": "Tawas City", "Iron": "Iron River",
    "Isabella": "Mount Pleasant", "Jackson": "Jackson", "Kalamazoo": "Kalamazoo",
    "Kalkaska": "Kalkaska", "Kent": "Grand Rapids", "Keweenaw": "Copper Harbor", "Lake": "Baldwin",
    "Lapeer": "Lapeer", "Leelanau": "Suttons Bay", "Lenawee": "Adrian", "Livingston": "Howell",
    "Luce": "Newberry", "Mackinac": "St. Ignace", "Macomb": "Mount Clemens",
    "Manistee": "Manistee", "Marquette": "Marquette", "Mason": "Ludington",
    "Mecosta": "Big Rapids", "Menominee": "Menominee", "Midland": "Midland",
    "Missaukee": "Lake City", "Monroe": "Monroe", "Montcalm": "Greenville",
    "Montmorency": "Atlanta", "Muskegon": "Muskegon", "Newaygo": "Newaygo", "Oakland": "Pontiac",
    "Oceana": "Hart", "Ogemaw": "West Branch", "Ontonagon": "Ontonagon", "Osceola": "Reed City",
    "Oscoda": "Mio", "Otsego": "Gaylord", "Ottawa": "Holland", "Presque Isle": "Rogers City",
    "Roscommon": "Houghton Lake", "Saginaw": "Saginaw", "St. Clair": "Port Huron",
    "St. Joseph": "Three Rivers", "Sanilac": "Sandusky", "Schoolcraft": "Manistique",
    "Shiawassee": "Owosso", "Tuscola": "Caro", "Van Buren": "South Haven",
    "Washtenaw": "Ann Arbor", "Wayne": "Detroit", "Wexford": "Cadillac",
}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", errors="replace")


def county_key(name):
    """Normalise a county name so AAA's and the Census's spellings match."""
    name = re.sub(r"\s+county$", "", name.strip(), flags=re.I).lower().replace(".", "")
    return re.sub(r"^saint ", "st ", name)


def place_label(state, county):
    if state != "MI":
        return f"{county} County, {state}"
    towns = {county_key(name): town for name, town in MI_TOWNS.items()}
    town = towns.get(county_key(county))
    return f"{county} County" + (f" ({town} area)" if town else "")


# ---------- stored trips ----------

def load_trips():
    return json.loads(TRIPS_PATH.read_text(encoding="utf-8")) if TRIPS_PATH.exists() else []


def save_trips(trips):
    TRIPS_PATH.parent.mkdir(exist_ok=True)
    TRIPS_PATH.write_text(json.dumps(trips, indent=1) + "\n", encoding="utf-8")


def active_trips(today):
    """Trips that haven't happened yet (kept through the day of travel)."""
    return [t for t in load_trips() if date.fromisoformat(t["date"]) >= today]


# ---------- building a route ----------

def geocode(place):
    query = urllib.parse.urlencode({"q": place, "format": "jsonv2", "limit": 5,
                                    "countrycodes": "us", "addressdetails": 1})
    hits = json.loads(_get("https://nominatim.openstreetmap.org/search?" + query))
    if not hits:
        raise RuntimeError(f"Couldn't find a place called '{place}'. Try adding the state, like 'Houghton, MI'.")
    # "Houghton, MI" matches both the county and the town; a trip goes to the town.
    towns = [h for h in hits if h.get("addresstype") in ("city", "town", "village", "hamlet", "suburb")]
    hit = (towns or hits)[0]
    state = hit.get("address", {}).get("state", "")
    return f"{hit['name']}, {state}".strip(", "), float(hit["lat"]), float(hit["lon"])


def county_at(lat, lon):
    """(state code, county name) for a point, or None over water / outside the US."""
    query = urllib.parse.urlencode({"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "json"})
    results = json.loads(_get("https://geo.fcc.gov/api/census/area?" + query)).get("results") or []
    if not results or not results[0].get("county_name"):
        return None
    return results[0]["state_code"], re.sub(r"\s+County$", "", results[0]["county_name"])


def _haversine_miles(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def legs_from_samples(samples, total_miles):
    """Merge route samples [(mile, (state, county) or None)] into consecutive county legs."""
    legs = []
    for mile, where in samples:
        if where is None:
            continue
        if legs and (legs[-1]["state"], legs[-1]["county"]) == where:
            legs[-1]["_last"] = mile
            continue
        start = 0.0
        if legs:
            start = (legs[-1]["_last"] + mile) / 2  # the county line is somewhere between samples
            legs[-1]["end"] = start
        legs.append({"state": where[0], "county": where[1], "start": start, "_last": mile})
    if not legs:
        raise RuntimeError("Couldn't work out which counties the route passes through")
    legs[-1]["end"] = total_miles
    return [{"state": l["state"], "county": l["county"],
             "start": round(l["start"], 1), "end": round(l["end"], 1)} for l in legs]


def plan_route(destination, home):
    """Driving route from home to `destination` as an ordered list of county legs."""
    name, lat, lon = geocode(destination)
    url = (f"https://router.project-osrm.org/route/v1/driving/{home['lon']},{home['lat']};{lon},{lat}"
           "?overview=full&geometries=geojson")
    reply = json.loads(_get(url))
    if reply.get("code") != "Ok":
        raise RuntimeError(f"Couldn't find a driving route to {name}")
    route = reply["routes"][0]
    coords = route["geometry"]["coordinates"]
    total = route["distance"] / 1609.344

    travelled = [0.0]
    for a, b in zip(coords, coords[1:]):
        travelled.append(travelled[-1] + _haversine_miles(a, b))
    scale = total / travelled[-1] if travelled[-1] else 1
    points, next_mile = [], 0.0
    for (plon, plat), t in zip(coords, travelled):
        if t * scale >= next_mile:
            points.append((t * scale, plat, plon))
            next_mile = t * scale + SAMPLE_MILES
    if points[-1][0] < total - 1:
        points.append((total, coords[-1][1], coords[-1][0]))

    samples = []
    for mile, plat, plon in points:
        samples.append((mile, county_at(plat, plon)))
        time.sleep(0.15)
    return {"destination": name, "miles": round(total), "hours": round(route["duration"] / 3600, 1),
            "legs": legs_from_samples(samples, total)}


# ---------- county prices ----------

def fetch_county_prices(state, state_html=None):
    """{county: regular $/gal} for every county in `state`, from AAA's state page map."""
    if state_html is None:
        state_html = _get(f"https://gasprices.aaa.com/?state={state}")
        time.sleep(AAA_CRAWL_DELAY)
    m = re.search(r"premiumhtml5map_js_data=true&(?:#038;|amp;)?map_id=(\d+)", state_html)
    if not m:
        raise RuntimeError(f"Couldn't find the county map on the AAA page for {state}")
    script = _get(f"https://gasprices.aaa.com/index.php?premiumhtml5map_js_data=true&map_id={m.group(1)}")
    prices = {name: float(price) for name, price in
              re.findall(r'"name":"([^"]+)"[^{}]*?"comment":"\$([\d.]+)"', script)}
    if not prices:
        raise RuntimeError(f"Couldn't read county prices for {state} (AAA layout may have changed)")
    return prices


# ---------- where to fill up ----------

def fuel_plan(stops, total_miles, cfg):
    """Cheapest practical way to buy gas along the route.

    stops: [{"name", "mile", "price"}] in route order, starting with home at mile 0.
    Minimises gas cost plus `trip_stop_cost` dollars per stop, so it won't send you to
    an extra pump to save a few cents. Assumes you start and finish at the reserve level.
    Returns [{"name", "mile", "gallons", "price", "cost", "fill"}], or None if some
    stretch is longer than the tank's range.
    """
    stops = [s for s in stops if s.get("price") is not None]
    if not stops or stops[0]["mile"] != 0:
        return None
    unit = 0.1  # gallons; the tank is tracked in tenths of a gallon above the reserve
    per_mile = 1 / cfg["trip_mpg"] / unit
    cap = round((cfg["tank_gallons"] - cfg["trip_reserve_gallons"]) / unit)
    where = [round(s["mile"] * per_mile) for s in stops] + [round(total_miles * per_mile)]

    # best[f] = (cost so far, purchases so far) on arriving at the current stop with f units
    best = {0: (0.0, ())}
    for i, stop in enumerate(stops):
        to_next = where[i + 1] - where[i]
        arrive = {}
        for fuel, (cost, bought) in best.items():
            for buy in range(0, cap - fuel + 1):
                left = fuel + buy - to_next
                if left < 0:
                    continue
                total = cost + buy * unit * stop["price"] + (cfg["trip_stop_cost"] if buy else 0)
                if left not in arrive or total < arrive[left][0]:
                    arrive[left] = (total, bought + ((i, buy, fuel + buy == cap),) if buy else bought)
        if not arrive:
            return None
        best = arrive
    _, bought = min(best.values(), key=lambda v: v[0])
    return [{"name": stops[i]["name"], "mile": round(stops[i]["mile"]), "price": stops[i]["price"],
             "gallons": round(buy * unit, 1), "cost": round(buy * unit * stops[i]["price"], 2),
             "fill": full} for i, buy, full in bought]


def summarize(trip, prices, changes, cfg):
    """Everything the page and the notification need for one trip.

    prices: {(state, county_key): $/gal} for today; changes: same keys -> 7-day change.
    """
    legs = []
    for leg in trip["legs"]:
        key = (leg["state"], county_key(leg["county"]))
        legs.append({**leg, "label": place_label(leg["state"], leg["county"]),
                     "price": prices.get(key), "change": changes.get(key)})
    priced = [l for l in legs if l["price"] is not None]
    summary = {"id": trip["id"], "destination": trip["destination"], "date": trip["date"],
               "miles": trip["miles"], "hours": trip.get("hours"), "legs": legs,
               "plan": None, "text": "No county prices for this route yet."}
    if not priced:
        return summary

    cheapest = min(priced, key=lambda l: l["price"])
    priciest = max(priced, key=lambda l: l["price"])
    for leg in legs:
        leg["tag"] = "cheapest" if leg is cheapest else "priciest" if leg is priciest else None

    # Home is the first county; every later county is a possible stop at its midpoint.
    stops = [{"name": "home", "mile": 0, "price": legs[0]["price"]}]
    stops += [{"name": l["label"], "mile": (l["start"] + l["end"]) / 2, "price": l["price"]} for l in legs[1:]]
    plan = fuel_plan(stops, trip["miles"], cfg)
    summary["plan"] = plan

    def mid(leg):
        return round((leg["start"] + leg["end"]) / 2)

    spread = priciest["price"] - cheapest["price"]
    text = ""
    if plan:
        steps = [("fill up" if b["fill"] else f"buy about {b['gallons']:.0f} gal")
                 + (" at home" if b["name"] == "home" else f" in {b['name']}") + f" (${b['price']:.2f})"
                 for b in plan]
        text = ", then ".join(steps) + ". "
        text = text[0].upper() + text[1:]
        text += f"About ${sum(b['cost'] for b in plan):.0f} in gas one way. "
    text += (f"Cheapest area on the route: {cheapest['label']} at ${cheapest['price']:.2f} (mile {mid(cheapest)}). "
             f"Priciest: {priciest['label']} at ${priciest['price']:.2f} (mile {mid(priciest)}), "
             f"{spread * 100:.0f}¢ more.")
    summary["text"] = text
    return summary
