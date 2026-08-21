"""
Local demo service: type an address (stand-in for a camera scan),
get back the resolved property's sale history plus nearby comparables,
built entirely from PPR. No BER data involved - that piece is still
blocked pending the SEAI/CSO requests.
"""
import datetime
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from importlib import import_module
normaliser = import_module("02_normalise_addresses")

# prefers the slimmed deploy.db (built for hosting, drops the unused BER
# tables and non-essential indexes to fit under GitHub's 100MB no-LFS
# limit) if present, otherwise the full local dev database
_deploy_db = ROOT / "data" / "processed" / "deploy.db"
_spike_db = ROOT / "data" / "processed" / "spike.db"
DB_PATH = Path(os.environ.get("DB_PATH", _deploy_db if _deploy_db.exists() else _spike_db))

# below this, the "best available" candidate is more likely noise than a
# real match - a wrong low-confidence answer is worse than admitting no match
MIN_MATCH_SCORE = 75

# flags bulk/institutional sales (whole apartment blocks sold to a single
# landlord, e.g. "comprising Block F and Block G") rather than a single
# dwelling. Calibrated against real PPR data: requires a plural unit word
# ("apartments", "units", "flats"...) next to a number range, not just any
# digit range - a bare range also matches ordinary single-unit sales where
# the building itself spans a few street numbers (e.g. "Apt 16, 16-18
# O'Connell St" is one flat, not a bulk sale). 299 rows match nationally,
# spot-checked clean from the smallest (a 4-cottage sale) to the largest
# (a 461-apartment block, EUR217m).
BULK_SALE_RE = re.compile(
    r"\b(apartments|units|flats|dwellings|cottages|blocks|no's|nos)\b[^,]{0,15}?\d+\s*-\s*\d+"
    r"|\bcomprising\b"
    r"|\bblocks\b",
    re.IGNORECASE,
)


def is_bulk_sale(address):
    return bool(BULK_SALE_RE.search(address))

app = Flask(__name__)
# SECRET_KEY should be set as an env var in production so sessions survive
# a redeploy; falls back to a random one (fine for local dev, means a
# restart logs everyone out, which is a non-issue for a personal tool)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# APP_PASSWORD gates the whole app behind a simple shared password when
# set (production). Unset (local dev/LAN testing) means no gate at all -
# deliberately opt-in, not opt-out, so local testing is never accidentally
# locked out by a stale cookie or missing env var.
APP_PASSWORD = os.environ.get("APP_PASSWORD")


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return None
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authed"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong password."
    return f"""
    <!doctype html><html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Kerbsight</title>
    <link rel="manifest" href="/static/manifest.json">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    <link rel="icon" type="image/png" href="/static/favicon-32.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Kerbsight">
    <meta name="theme-color" content="#0a0a0a">
    </head>
    <body style="font-family:-apple-system,sans-serif;max-width:320px;margin:80px auto">
    <h1 style="font-size:20px;margin:0 0 16px;">Kerbsight</h1>
    <form method="post">
      <p>{error or ''}</p>
      <input type="password" name="password" placeholder="Password" autofocus
             style="width:100%;padding:10px;font-size:15px;box-sizing:border-box">
      <button style="margin-top:8px;padding:8px 16px">Enter</button>
    </form>
    </body></html>
    """, (401 if error else 200)


COUNTIES = [
    "Carlow", "Cavan", "Clare", "Cork", "Donegal", "Dublin", "Galway",
    "Kerry", "Kildare", "Kilkenny", "Laois", "Leitrim", "Limerick",
    "Longford", "Louth", "Mayo", "Meath", "Monaghan", "Offaly",
    "Roscommon", "Sligo", "Tipperary", "Waterford", "Westmeath",
    "Wexford", "Wicklow",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_price_index():
    """
    County x year median price, for scaling a property's last known
    sale price up to a present-day estimate - the same family of method
    the CSO's own Residential Property Price Index uses (a repeat-sales/
    index approach), not a per-property valuation model.

    Excludes bulk/institutional sales (a single EUR50m+ block sale would
    distort even a median at county level) and rows PPR itself flags as
    "Not Full Market Price" (gifts, family transfers, etc - not
    arm's-length transactions, so not representative of market value).

    Checked before building this: every (county, year) bucket has at
    least 116 sales after those exclusions (smallest was Monaghan 2011),
    most have hundreds to tens of thousands - a county-level yearly
    median is statistically reasonable everywhere, no sparse-data
    fallback needed.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT county, date_of_sale, price_eur, address FROM ppr_raw WHERE not_full_market_price = 'No'"
    ).fetchall()
    conn.close()

    by_bucket = defaultdict(list)
    for r in rows:
        if is_bulk_sale(r["address"]):
            continue
        year = r["date_of_sale"][-4:]
        by_bucket[(r["county"], year)].append(r["price_eur"])

    index = defaultdict(dict)
    for (county, year), prices in by_bucket.items():
        index[county][year] = {"median": statistics.median(prices), "n": len(prices)}
    return index


PRICE_INDEX = build_price_index()


def build_ppr_coverage():
    """
    The actual earliest and latest sale dates present in PPR, computed
    from the data rather than hardcoded - so if the underlying extract
    is refreshed with a later cutoff, this updates automatically rather
    than silently going stale. Used to explain a missing sale honestly:
    a property showing no PPR history might genuinely not have sold
    within the register's own coverage window, not be a lookup failure.
    """
    conn = get_db()
    row = conn.execute(
        """
        SELECT
          MIN(substr(date_of_sale,7,4) || substr(date_of_sale,4,2) || substr(date_of_sale,1,2)) AS min_sortable,
          MAX(substr(date_of_sale,7,4) || substr(date_of_sale,4,2) || substr(date_of_sale,1,2)) AS max_sortable
        FROM ppr_raw
        """
    ).fetchone()
    conn.close()

    month_names = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]

    def to_month_year(sortable):
        year, month = sortable[:4], sortable[4:6]
        return f"{month_names[int(month)]} {year}"

    return {
        "start_year": row["min_sortable"][:4],
        "start_label": to_month_year(row["min_sortable"]),
        "latest_label": to_month_year(row["max_sortable"]),
    }


PPR_COVERAGE = build_ppr_coverage()


def estimate_current_value(county, last_price, last_sale_year):
    """
    Scale a property's last known sale price by how much the county-level
    median has moved between the sale year and the latest year the index
    covers. Returns None if there isn't enough index coverage to do this
    (shouldn't happen given the density check above, but a real product
    should never silently substitute a guess for a missing lookup).
    """
    county_index = PRICE_INDEX.get(county)
    if not county_index or last_sale_year not in county_index:
        return None

    latest_year = max(county_index)
    base = county_index[last_sale_year]
    latest = county_index[latest_year]

    ratio = latest["median"] / base["median"]

    # annualized (CAGR) rather than the raw total-change percentage above -
    # undefined for a same-year sale (0 years held), so left as None rather
    # than a divide-by-zero or a misleading "0% annualized"
    years_held = int(latest_year) - int(last_sale_year)
    annualized_change_pct = (
        round(((ratio ** (1 / years_held)) - 1) * 100, 2) if years_held > 0 else None
    )

    trend_years = sorted(county_index.keys())[-10:]
    price_trend = [
        {"year": y, "median": county_index[y]["median"], "n": county_index[y]["n"]}
        for y in trend_years
    ]

    return {
        "estimated_value": round(last_price * ratio, 2),
        "based_on_sale_year": last_sale_year,
        "index_latest_year": latest_year,
        "county_price_change_pct": round((ratio - 1) * 100, 1),
        "annualized_change_pct": annualized_change_pct,
        "price_trend": price_trend,
        "index_sample_sizes": {last_sale_year: base["n"], latest_year: latest["n"]},
    }


EARTH_RADIUS_M = 6371000
# how far ahead of the camera to project, in metres - a rough stand-in for
# "the building you're facing" rather than "the building you're standing
# in front of the gate of". No building-footprint data to do this properly
# (see report section 12c/15) - this is a straight-line guess along the
# compass bearing, nothing more.
SIGHT_DISTANCE_M = 20


def project_point(lat, lon, bearing_deg, distance_m=SIGHT_DISTANCE_M):
    """
    Given a position and a compass bearing, return the point
    `distance_m` metres ahead along that bearing - standard great-circle
    destination-point formula. This is what turns "where is the phone"
    into "what is the phone facing", which GPS alone cannot answer.
    """
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


NOMINATIM_UA = "ppr-ber-spike-feasibility-research/0.1 (single on-demand lookups only)"


def _nominatim_get(url, timeout=8, retries=1):
    """
    GET with a single retry on HTTP 429, honouring Retry-After when
    Nominatim sends one. Confirmed live this is a real, user-facing
    failure mode, not just test-time rate limiting: Render's free-tier
    outbound IPs are shared across many tenants, so a single well-paced
    request from this app can still land in the 429 bucket if Nominatim's
    per-IP limit was already spent by someone else's traffic through the
    same address. One retry after a short pause is enough to ride out a
    transient hit without turning into the kind of repeated/looped
    calling the usage policy actually forbids.
    """
    req = urllib.request.Request(url, headers={"User-Agent": NOMINATIM_UA})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 1.5
                time.sleep(min(delay, 5))
                continue
            raise


def reverse_geocode(lat, lon):
    """
    Turn a GPS coordinate into a street address via OpenStreetMap's free
    Nominatim service. This is what a 'point your camera' feature would
    call after resolving device position - it does NOT know which
    specific building you're facing, just which address is nearest.
    Single on-demand lookups only: Nominatim's usage policy forbids bulk
    use without a separate agreement, and this app never calls it in a
    loop over PPR rows, only once per user action.
    """
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "json", "addressdetails": 1,
    })
    data = _nominatim_get(url)

    addr = data.get("address", {})
    house_no = addr.get("house_number", "")
    road = addr.get("road", "")
    town = addr.get("town") or addr.get("suburb") or addr.get("city_district") or ""
    county_raw = addr.get("county", "")
    county = county_raw.replace("County ", "").strip() or None

    query_parts = [p for p in [f"{house_no} {road}".strip(), town] if p]
    query_text = ", ".join(query_parts)
    return {
        "query_text": query_text,
        "county": county,
        "locality": town or None,
        "raw_display_name": data.get("display_name"),
    }


# Building-footprint identification: rather than blindly projecting 20m
# along the compass bearing and reverse-geocoding whatever point that
# lands on (see SIGHT_DISTANCE_M above), this casts the same bearing as
# an actual ray and finds which real OSM building polygon it hits first -
# a geometric answer to "which building is this", not a guess. Falls
# back to the projection method (further down in api_nearby) whenever no
# building with usable address tags is found, so this can only improve
# results, never make them worse. Verified live before building: OSM has
# real building polygons with addr:housenumber/addr:street tags for the
# Dublin streets this app has been tested against.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BUILDING_SEARCH_RADIUS_M = 50


def latlon_to_local_xy(lat, lon, ref_lat, ref_lon):
    """Flat-earth approximation in metres, x=east, y=north - fine at the
    tens-of-metres scale a building sighting operates at, not for anything
    longer range."""
    x = (lon - ref_lon) * 111320 * math.cos(math.radians(ref_lat))
    y = (lat - ref_lat) * 110540
    return x, y


def local_xy_to_latlon(x, y, ref_lat, ref_lon):
    lon = ref_lon + x / (111320 * math.cos(math.radians(ref_lat)))
    lat = ref_lat + y / 110540
    return lat, lon


def ray_segment_intersection(ray_origin, ray_dir_unit, seg_a, seg_b):
    """
    Distance along the ray (ray_dir_unit must be a unit vector) to where
    it crosses segment seg_a->seg_b, or None if it doesn't. Standard
    parametric line-intersection algebra.
    """
    ox, oy = ray_origin
    dx, dy = ray_dir_unit
    ax, ay = seg_a
    bx, by = seg_b
    sx, sy = bx - ax, by - ay
    denom = dx * sy - dy * sx
    if abs(denom) < 1e-9:
        return None
    t = ((ax - ox) * sy - (ay - oy) * sx) / denom
    u = ((ax - ox) * dy - (ay - oy) * dx) / denom
    if t >= 0 and 0 <= u <= 1:
        return t
    return None


def fetch_nearby_buildings(lat, lon, radius_m=BUILDING_SEARCH_RADIUS_M):
    """
    OSM building polygons within radius_m, via the free Overpass API.
    Same single-on-demand-call policy as Nominatim above - one call per
    sighting, never batched or looped.
    """
    query = f'[out:json][timeout:15];way["building"](around:{radius_m},{lat},{lon});out geom;'
    req = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": NOMINATIM_UA},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("elements", [])


BUILDING_LOCALITY_MAX_DISTANCE_M = 250


def guess_locality_for_street(county, house_no, street_name, hit_lat, hit_lon):
    """
    Finds which PPR locality genuinely corresponds to the ray-cast
    building, by forward-geocoding each distinct (house_no, street,
    locality) PPR entry that shares this house number and street name
    and comparing the result to the real hit point - not just trusting
    the first text match.

    Necessary because street names collide across unrelated areas
    within the same county: Dublin has more than one "Brighton Road"
    (Rathgar and Foxrock are different places, several km apart), and
    an earlier version of this that just picked PPR's first matching
    row confidently resolved a real Rathgar building to an unrelated
    Foxrock sale that happened to share the same house number.

    Returns None if no PPR entry is within BUILDING_LOCALITY_MAX_DISTANCE_M
    of the actual building - meaning it most likely has no PPR sale on
    record at all, a real possible outcome that should fall through to
    an honest "no confident match" rather than a wrong answer.
    """
    if not county or not house_no or not street_name:
        return None
    street_norm = normaliser.fold(normaliser.expand_abbreviations(normaliser.lower_and_tidy(street_name)))
    street_norm = re.sub(r'[^a-z0-9 ]', '', street_norm).strip()
    if not street_norm:
        return None

    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT locality FROM ppr_normalised WHERE county = ? AND house_no = ? AND canonical LIKE ? AND locality <> ''",
        (county, str(house_no), f"%{street_norm}%"),
    ).fetchall()
    conn.close()
    if not rows:
        return None

    best_locality = None
    best_dist = None
    for row in rows:
        try:
            glat, glon = forward_geocode(f"{house_no} {street_name}, {row['locality']}")
        except Exception:
            continue
        if glat is None:
            continue
        dx, dy = latlon_to_local_xy(glat, glon, hit_lat, hit_lon)
        dist = math.hypot(dx, dy)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_locality = row["locality"]

    if best_locality is not None and best_dist <= BUILDING_LOCALITY_MAX_DISTANCE_M:
        return best_locality
    return None


def find_building_along_bearing(lat, lon, bearing_deg, max_distance_m=BUILDING_SEARCH_RADIUS_M):
    """
    Casts a ray from (lat, lon) along bearing_deg and returns the tags,
    distance, and lat/lon of the nearest OSM building polygon it crosses
    that has both addr:housenumber and addr:street set - skipping past
    any closer but untagged buildings, since an untagged hit is no more
    useful than no hit at all. Returns (None, None, None, None) if
    nothing usable is found or the Overpass call fails.
    """
    try:
        buildings = fetch_nearby_buildings(lat, lon, radius_m=max_distance_m)
    except Exception:
        return None, None, None, None

    dir_x = math.sin(math.radians(bearing_deg))
    dir_y = math.cos(math.radians(bearing_deg))
    origin = (0.0, 0.0)

    hits = []
    for element in buildings:
        geom = element.get("geometry")
        tags = element.get("tags") or {}
        if not geom or len(geom) < 2:
            continue
        pts = [latlon_to_local_xy(pt["lat"], pt["lon"], lat, lon) for pt in geom]
        best_for_this_building = None
        for i in range(len(pts) - 1):
            dist = ray_segment_intersection(origin, (dir_x, dir_y), pts[i], pts[i + 1])
            if dist is not None and dist <= max_distance_m:
                if best_for_this_building is None or dist < best_for_this_building:
                    best_for_this_building = dist
        if best_for_this_building is not None:
            hits.append((best_for_this_building, tags))

    hits.sort(key=lambda h: h[0])
    for dist, tags in hits:
        if tags.get("addr:housenumber") and tags.get("addr:street"):
            hit_x = dir_x * dist
            hit_y = dir_y * dist
            hit_lat, hit_lon = local_xy_to_latlon(hit_x, hit_y, lat, lon)
            return tags, dist, hit_lat, hit_lon
    return None, None, None, None


def forward_geocode(query_text):
    """
    The inverse of reverse_geocode: turns a resolved PPR address into a GPS
    coordinate via Nominatim, so a typed search can also drive the
    geometry-based lookups below (zoning, planning applications, flood
    risk, transit) - lookups a GPS-based search gets for free from the
    device's own position. Same single on-demand usage policy as
    reverse_geocode: one call per typed search that resolves to a
    confident match, never batched or looped.
    """
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": f"{query_text}, Ireland", "format": "json", "limit": 1,
    })
    results = _nominatim_get(url)
    if not results:
        return None, None
    return float(results[0]["lat"]), float(results[0]["lon"])


def _arcgis_query(url, lat, lon, out_fields, distance_m=None, extra_params=None, timeout=6):
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "f": "json",
    }
    if distance_m:
        params["distance"] = distance_m
        params["units"] = "esriSRUnit_Meter"
    if extra_params:
        params.update(extra_params)
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": NOMINATIM_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# All four confirmed live against real coordinates before being wired in -
# same "verify before building" standard as PPR/BER: point queries at a
# known Dublin address (75 Orwell Road / Marianella) each returned real,
# checkable data, not placeholders.
ARCGIS_ZONING_URL = "https://services.arcgis.com/NzlPQPKn5QF9v2US/ArcGIS/rest/services/GZT_Current_Plan/FeatureServer/0/query"
ARCGIS_PLANNING_URL = "https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/IrishPlanningApplications/FeatureServer/0/query"
ARCGIS_FLOOD_URL = "https://services6.arcgis.com/MmUrOQU5v1he9gfS/arcgis/rest/services/NIFM_River_Flood_Extents_OPW_Ireland_2020/FeatureServer/{layer}/query"
ARCGIS_PTAL_URL = "https://services-eu1.arcgis.com/p0UmGrpumWZYhF0p/ArcGIS/rest/services/PTAL_8am_to_9am/FeatureServer/1/query"


def _epoch_ms_to_date(ms):
    if not ms:
        return None
    return datetime.date.fromtimestamp(ms / 1000).isoformat()


def zoning_lookup(lat, lon):
    """
    Point-in-polygon against DHLGH's Generalised Zoning Types layer - a
    standardised composite of local authority development-plan zoning
    (source: MyPlan.ie / data.gov.ie). Returns None on any failure or if
    the point falls outside mapped/zoned land (e.g. some rural areas) -
    an absent zone is a real possible outcome, not an error to hide.
    """
    try:
        data = _arcgis_query(
            ARCGIS_ZONING_URL, lat, lon,
            out_fields="ZONE_GZT,GZT_DESC,ZONE_DESC,ZONE_ORIG,LA_NAME,PLAN_NAME,PLAN_LEVEL",
        )
        feats = data.get("features", [])
        if not feats:
            return None
        a = feats[0]["attributes"]
        return {
            "zone_code": a.get("ZONE_GZT"),
            "zone_category": a.get("GZT_DESC"),
            "zone_description": a.get("ZONE_DESC") or a.get("ZONE_ORIG"),
            "local_authority": a.get("LA_NAME"),
            "plan_name": a.get("PLAN_NAME"),
            "source": "DHLGH Generalised Development Zoning",
        }
    except Exception:
        return None


def planning_applications_lookup(lat, lon, radius_m=300, limit=5):
    """
    Nearby planning applications within radius_m, most recent first.
    Source: local authority planning application data aggregated on
    ArcGIS (the same feed that, tested live, correctly surfaced the
    user's own building's actual planning history).
    """
    try:
        data = _arcgis_query(
            ARCGIS_PLANNING_URL, lat, lon,
            out_fields="DevelopmentDescription,DevelopmentAddress,ApplicationStatus,"
                        "Decision,ReceivedDate,DecisionDate,NumResidentialUnits",
            distance_m=radius_m,
            extra_params={"orderByFields": "ReceivedDate DESC", "resultRecordCount": limit},
        )
        out = []
        for f in data.get("features", []):
            a = f["attributes"]
            out.append({
                "description": a.get("DevelopmentDescription"),
                "address": a.get("DevelopmentAddress"),
                "status": a.get("ApplicationStatus"),
                "decision": a.get("Decision"),
                "received_date": _epoch_ms_to_date(a.get("ReceivedDate")),
                "decision_date": _epoch_ms_to_date(a.get("DecisionDate")),
                "residential_units": a.get("NumResidentialUnits"),
            })
        return out
    except Exception:
        return []


# river flood extent layers on the OPW's National Indicative Fluvial
# Mapping service, checked worst-case-first: if a point falls inside the
# more frequent 100-year extent it's also inside the 1000-year one, so
# stop at the first hit rather than querying all nine layers
FLOOD_LAYERS = [
    (3, "100-year river flood extent (current climate, ~1% annual chance)"),
    (4, "1000-year river flood extent (current climate, ~0.1% annual chance)"),
]


def flood_risk_lookup(lat, lon):
    try:
        for layer_id, label in FLOOD_LAYERS:
            data = _arcgis_query(
                ARCGIS_FLOOD_URL.format(layer=layer_id), lat, lon, out_fields="FID",
            )
            if data.get("features"):
                return {
                    "in_mapped_flood_extent": True,
                    "extent_matched": label,
                    "source": "OPW National Indicative Fluvial Mapping (river flooding only - "
                              "does not cover coastal or pluvial/surface-water flooding)",
                }
        return {
            "in_mapped_flood_extent": False,
            "extent_matched": None,
            "source": "OPW National Indicative Fluvial Mapping (river flooding only - "
                      "does not cover coastal or pluvial/surface-water flooding)",
        }
    except Exception:
        return None


def transit_lookup(lat, lon):
    """
    Public Transport Accessibility Level for the 8-9am peak, published by
    the National Transport Authority - a composite score (walk time to
    stops x service frequency), not a raw stop-distance count, so it
    already accounts for how useful nearby transit actually is.
    """
    try:
        data = _arcgis_query(ARCGIS_PTAL_URL, lat, lon, out_fields="PTAL_Score,Time_Period")
        feats = data.get("features", [])
        if not feats:
            return None
        a = feats[0]["attributes"]
        return {
            "ptal_score": a.get("PTAL_Score"),
            "time_period": a.get("Time_Period"),
            "source": "National Transport Authority PTAL",
        }
    except Exception:
        return None


NIAH_URL = "https://services-eu1.arcgis.com/HyjXgkV6KGMSF3jt/arcgis/rest/services/NIAHBuildingsOpenData/FeatureServer/0/query"
NIAH_SEARCH_RADIUS_M = 30


def heritage_lookup(lat, lon):
    """
    National Inventory of Architectural Heritage record within a tight
    radius of the point - confirmed live against real per-house-number
    entries (e.g. individual Georgian houses on Merrion Square, each
    with its own rating). NIAH is a heritage *survey*, not a legal
    status: being recorded here is what feeds a local authority's own
    Record of Protected Structures, but isn't the same as being on it -
    said explicitly in the returned source note rather than implied.
    """
    try:
        data = _arcgis_query(
            NIAH_URL, lat, lon,
            out_fields="REG_NO,NAME,ORIGINAL_TYPE,IN_USE_AS_TYPE,RATING",
            distance_m=NIAH_SEARCH_RADIUS_M,
        )
        feats = data.get("features", [])
        if not feats:
            return None
        a = feats[0]["attributes"]
        return {
            "reg_no": a.get("REG_NO"),
            "name": a.get("NAME"),
            "original_use": a.get("ORIGINAL_TYPE"),
            "current_use": a.get("IN_USE_AS_TYPE"),
            "rating": a.get("RATING"),
            "source": "National Inventory of Architectural Heritage (NIAH) - a heritage survey "
                      "record, not itself a statement of legal protection. Check the local "
                      "authority's own Record of Protected Structures for that.",
        }
    except Exception:
        return None


def geo_context(lat, lon):
    if lat is None or lon is None:
        return None
    return {
        "zoning": zoning_lookup(lat, lon),
        "planning_applications": planning_applications_lookup(lat, lon),
        "flood_risk": flood_risk_lookup(lat, lon),
        "heritage": heritage_lookup(lat, lon),
        "transit": transit_lookup(lat, lon),
    }


def detect_county(query_text):
    lowered = query_text.lower()
    for c in COUNTIES:
        if c.lower() in lowered:
            return c
    return None


def load_candidates(conn, county):
    if county:
        rows = conn.execute(
            "SELECT ppr_id, canonical, house_no, locality FROM ppr_normalised WHERE county = ? AND canonical <> ''",
            (county,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ppr_id, canonical, house_no, locality FROM ppr_normalised WHERE canonical <> ''"
        ).fetchall()
    return rows


def street_portion(canonical, house_no, locality):
    """
    Strip house number and locality off a canonical string, leaving just
    the street name tokens. Needed because many Dublin streets are named
    after their own suburb ("Rathgar Road" in Rathgar, "Dundrum Road" in
    Dundrum) - comparing whole canonical strings lets the locality word
    count twice and inflate similarity between two completely different
    streets that happen to both mention the suburb name. Found this live:
    "59 Orwell Road, Rathgar" matched "59 Rathgar Road, Rathgar" at 73/100
    (above the old 65 guard) purely because "rathgar" appears twice in
    the candidate's own canonical string.
    """
    s = canonical
    if house_no and s.startswith(house_no):
        s = s[len(house_no):].strip()
    if locality and s.endswith(locality):
        s = s[: len(s) - len(locality)].strip()
    return s


def extract_street_name(raw_address, county):
    """
    First comma-separated segment after the house number, e.g. "brighton
    rd" from "75 Brighton Rd, Rathgar, Dublin 6". Used to scope
    comparables to the actual street rather than the `locality` field,
    which can span a whole postal district (Dublin 6 alone covers dozens
    of streets) and - since it's just whichever comma segment happens to
    land last - isn't even consistently the same segment across two
    sales on the same street if their raw addresses have a different
    number of comma-separated parts.
    """
    working = normaliser.lower_and_tidy(raw_address)
    working = normaliser.strip_county_suffix(working, county)
    _, remainder = normaliser.extract_house_no(working)
    remainder = normaliser.expand_abbreviations(remainder)
    remainder = re.sub(r'\s+', ' ', remainder).strip(' ,')
    tokens = [t.strip() for t in remainder.split(',') if t.strip()]
    if not tokens:
        return None
    street = normaliser.fold(tokens[0])
    street = re.sub(r'[^a-z0-9 ]', '', street).strip()
    return street or None


UNIT_KEYWORD_RE = re.compile(r'^\s*(?:apt|apartment|unit|flat)\.?\s*', re.IGNORECASE)


def extract_unit_and_building(raw_address):
    """
    Unit number and the building name remainder, e.g. ("35", "Orwell
    Hall, Marianella, Rathgar") from "UNIT 35 ORWELL HALL, MARIANELLA,
    RATHGAR". Reuses extract_house_no after stripping an optional
    leading APT/UNIT/FLAT keyword - PPR records the same building's
    units inconsistently (some rows lead with "UNIT 7", others with a
    bare "7 Orwell Hall"), so both forms are treated the same way.
    """
    s = UNIT_KEYWORD_RE.sub('', raw_address.strip())
    unit_no, remainder = normaliser.extract_house_no(s)
    return unit_no, remainder.strip(' ,')


def building_key(remainder):
    """
    First one or two words of a building-name remainder, used as a
    LIKE search key. Kept short deliberately - PPR formats the rest of
    the address (block letters, comma placement) inconsistently across
    units in the same building ("Orwell Hall Block C, Marianella" vs
    "Orwell Hall, Block C, Marianella"), so anchoring on more than the
    building's own name stops matching across those variants.
    """
    words = re.findall(r"[A-Za-z']+", remainder)
    if not words:
        return None
    key = " ".join(words[:2]) if len(words) >= 2 else words[0]
    return key if len(key) >= 5 else None


def building_breakdown(county, raw_address):
    """
    Every other unit PPR has on record in the same named building,
    keyed off the building name rather than the street - a Dublin
    apartment block's units don't share a house number the way a
    single house's own sale history does. Returns None for anything
    that isn't itself a flagged unit (a standalone house has no
    "building" to break down) or where fewer than two units are found.
    """
    unit_no, remainder = extract_unit_and_building(raw_address)
    if not unit_no:
        return None
    key = building_key(remainder)
    if not key:
        return None

    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT r.address, r.date_of_sale, r.price_eur FROM ppr_raw r
        WHERE r.county = ? AND r.address LIKE ?
        ORDER BY {DATE_SORT_SQL}
        """,
        (county, f"%{key}%"),
    ).fetchall()
    conn.close()

    units = {}
    for r in rows:
        u_no, _ = extract_unit_and_building(r["address"])
        if not u_no:
            continue
        units[u_no] = {
            "unit": u_no,
            "date_of_sale": r["date_of_sale"],
            "price_eur": r["price_eur"],
            "raw_address": r["address"],
        }
    if len(units) < 2:
        return None

    def sort_key(u):
        m = re.match(r'(\d+)', u["unit"])
        return (int(m.group(1)) if m else 0, u["unit"])

    return sorted(units.values(), key=sort_key)


def search_address(query_text, top_n=5):
    """
    Two stages, not one flat fuzzy match on the whole address string.

    A single token_sort_ratio pass over the full canonical string treats
    the house number as just another token, so a query missing one
    locality word (e.g. a reverse-geocoded point missing "Milltown")
    can rank a wrong house on a similarly-named street above the right
    house on the correct one - confidently. Found this by testing a
    real coordinate, not by inspection.

    Stage 1: fuzzy-match locality only, to find the right street/area.
    Stage 2: within that locality, look for an exact house-number match.
    If found, that's a building-level result. If not, fall back to the
    best whole-string fuzzy match and say so explicitly - a street-level
    guess should never be presented with the same confidence as a
    confirmed house number.
    """
    conn = get_db()
    county = detect_county(query_text)
    n = normaliser.normalise_address(query_text, county)
    query_canonical = n["canonical"]
    query_house_no = n["house_no"]
    query_locality = n["locality"]

    candidates = load_candidates(conn, county)
    conn.close()
    if not candidates:
        return None, county, []

    canon_list = [r["canonical"] for r in candidates]

    if query_house_no and query_locality:
        localities = list({r["locality"] for r in candidates if r["locality"]})
        loc_matches = process.extract(
            query_locality, localities, scorer=fuzz.token_sort_ratio, limit=3
        )
        candidate_localities = {loc for loc, score, _ in loc_matches if score >= MIN_MATCH_SCORE}
        if candidate_localities:
            exact = [
                r for r in candidates
                if r["locality"] in candidate_localities and r["house_no"] == query_house_no
            ]
            if exact:
                # multiple exact matches (e.g. same house number, different
                # sub-estates within the matched localities) - take the one
                # whose full canonical string is closest to the query
                query_street = street_portion(query_canonical, query_house_no, query_locality)
                best = max(
                    exact,
                    key=lambda r: fuzz.token_sort_ratio(
                        query_street, street_portion(r["canonical"], r["house_no"], r["locality"])
                    ),
                )
                best_street = street_portion(best["canonical"], best["house_no"], best["locality"])
                street_similarity = fuzz.token_sort_ratio(query_street, best_street)
                # An exact house-number hit inside a coarse locality (e.g.
                # "Dundrum" covering dozens of streets) can land on the right
                # number on entirely the wrong street - found this twice by
                # testing real coordinates: "62 Churchfields" resolved to "62
                # Beech Lawn" (both house 62 in Dundrum), then "59 Orwell
                # Road" resolved to "59 Rathgar Road" (both house 59 in
                # Rathgar - and comparing whole strings let "Rathgar" count
                # twice and inflate the score past the first guard). Compare
                # street name only, with the locality stripped from both
                # sides, before trusting an exact house-number hit.
                if street_similarity >= 55:
                    return {
                        "canonical": best["canonical"],
                        "score": 100.0,
                        "county": county,
                        "house_confirmed": True,
                    }, county, []

    if query_house_no:
        # The locality stage above depends on the query's locality text
        # fuzzy-matching something in PPR's own locality vocabulary - but
        # a GPS-derived query's "locality" is whatever Nominatim's
        # city_district/suburb happens to be, which is sometimes an
        # electoral ward name with no resemblance to any real PPR
        # locality (found live: "Rathmines West C Ward 1986" scored only
        # 68/100 against every real Dublin locality, well under the
        # match floor, so the stage above never ran at all). When that
        # happens this fell straight through to a whole-string fuzzy
        # match, which barely weights the house number against 4-5
        # other matching tokens - and confidently resolved "5 Palmerston
        # Park" to an unrelated "19 Palmerston Park" sale. House number
        # plus street name alone, with no locality corroboration, is
        # still a strong enough signal on its own: try it before falling
        # back to a plain fuzzy match with no house-number weighting.
        house_matches = [r for r in candidates if r["house_no"] == query_house_no]
        if house_matches:
            query_street = street_portion(query_canonical, query_house_no, query_locality)
            # token_set_ratio, not token_sort_ratio - PPR's own locality
            # field is inconsistently extracted (some records leave a
            # real locality word, like "Rathmines", stuck onto the
            # street portion instead of in the locality field), so the
            # query's cleanly-stripped street name is often a strict
            # subset of the candidate's messier one. token_sort_ratio
            # penalises that missing word as if it were a mismatch,
            # scoring the correct "Palmerston Park, Rathmines" (72) lower
            # than an unrelated "Palmerstown Drive" in a different part
            # of Dublin (74) - found live, both fell under the old
            # threshold anyway, so it silently fell through and mismatched
            # to a different house number entirely. token_set_ratio scores
            # subset containment near 100 while still correctly scoring
            # "Palmerstown" (a genuinely different street) well below it.
            best = max(
                house_matches,
                key=lambda r: fuzz.token_set_ratio(
                    query_street, street_portion(r["canonical"], r["house_no"], r["locality"])
                ),
            )
            best_street = street_portion(best["canonical"], best["house_no"], best["locality"])
            street_similarity = fuzz.token_set_ratio(query_street, best_street)
            # Stricter than the locality-corroborated stage above (55)
            # since there's no second signal backing this one up.
            if street_similarity >= 90:
                return {
                    "canonical": best["canonical"],
                    "score": 100.0,
                    "county": county,
                    "house_confirmed": True,
                }, county, []

    matches = process.extract(
        query_canonical, canon_list, scorer=fuzz.token_sort_ratio, limit=top_n
    )
    if not matches:
        return None, county, []

    best_canonical, score, idx = matches[0]
    return {
        "canonical": best_canonical,
        "score": score,
        "county": county,
        "house_confirmed": False,
    }, county, matches


# date_of_sale is stored as literal "DD/MM/YYYY" text. Sorting that
# lexicographically sorts by day-of-month first, not year - e.g.
# "05/09/2017" sorts before "31/03/2017" even though March comes before
# September. Rebuild it as YYYYMMDD for every ORDER BY that needs actual
# chronological order (which "most recent sale" for a valuation
# absolutely does).
DATE_SORT_SQL = "substr(r.date_of_sale,7,4) || substr(r.date_of_sale,4,2) || substr(r.date_of_sale,1,2)"


def property_report(canonical, county, lat=None, lon=None):
    conn = get_db()
    history = conn.execute(
        f"""
        SELECT r.date_of_sale, r.price_eur, r.address, r.description, r.size_description
        FROM ppr_normalised n JOIN ppr_raw r ON r.id = n.ppr_id
        WHERE n.county = ? AND n.canonical = ?
        ORDER BY {DATE_SORT_SQL}
        """,
        (county, canonical),
    ).fetchall()

    locality_row = conn.execute(
        "SELECT locality, house_no FROM ppr_normalised WHERE county = ? AND canonical = ? LIMIT 1",
        (county, canonical),
    ).fetchone()
    locality = locality_row["locality"] if locality_row else None

    # comparables are scoped to the same street, not the same `locality`
    # field - a Dublin postal district like "Dublin 6" covers dozens of
    # streets, so locality alone let entirely unrelated streets show up
    # as "nearby". Street name comes from the raw address (comma
    # structure is lost once it's folded into `canonical`), matched via
    # LIKE against canonical, which already has abbreviations normalised
    # the same way (road -> rd etc) so "Brighton Road" and "Brighton Rd"
    # agree.
    # locality is still required alongside the street-name match, not
    # dropped in favour of it - Ireland has plenty of same-named streets
    # in different areas of the same county (there's a "Brighton Road"
    # in both Rathgar and Foxrock, both Dublin), and a bare county-wide
    # LIKE match found this the hard way, pulling in a Foxrock apartment
    # block as a "comparable" for a Rathgar house
    street_name = extract_street_name(history[0]["address"], county) if history else None
    comparables = []
    if street_name and locality:
        # matched against the START of each candidate's own remainder
        # (house number, then street name), not just anywhere in its
        # canonical string - a plain "contains" match let properties on a
        # different street that merely mention this one in passing (e.g.
        # "49 Merton Dr, Sandford Rd, Dublin 6") show up as comparables
        comparables = conn.execute(
            f"""
            SELECT r.date_of_sale, r.price_eur, r.address
            FROM ppr_normalised n JOIN ppr_raw r ON r.id = n.ppr_id
            WHERE n.county = ? AND n.locality = ? AND n.canonical <> ?
              AND n.canonical LIKE (n.house_no || ' ' || ? || '%')
            ORDER BY {DATE_SORT_SQL} DESC
            LIMIT 10
            """,
            (county, locality, canonical, street_name),
        ).fetchall()

    # locality-level median/sample size - now a dedicated query rather
    # than reusing the comparables list, since comparables are street-
    # scoped and would otherwise understate the locality's real sample
    locality_stats = None
    if locality:
        locality_rows = conn.execute(
            """
            SELECT r.price_eur, r.address
            FROM ppr_normalised n JOIN ppr_raw r ON r.id = n.ppr_id
            WHERE n.county = ? AND n.locality = ?
            """,
            (county, locality),
        ).fetchall()
        locality_prices = [r["price_eur"] for r in locality_rows if not is_bulk_sale(r["address"])]
        if locality_prices:
            locality_stats = {
                "median_price": statistics.median(locality_prices),
                "n_sales": len(locality_prices),
            }

    conn.close()

    def tag(row):
        d = dict(row)
        d["is_bulk_sale"] = is_bulk_sale(d["address"])
        return d

    tagged_history = [tag(h) for h in history]
    tagged_comparables = [tag(c) for c in comparables]
    valuation = None
    if tagged_history:
        latest_sale = tagged_history[-1]
        if not latest_sale["is_bulk_sale"]:
            sale_year = latest_sale["date_of_sale"][-4:]
            valuation = estimate_current_value(county, latest_sale["price_eur"], sale_year)

    geo = geo_context(lat, lon)
    breakdown = building_breakdown(county, history[0]["address"]) if history else None

    return {
        "history": tagged_history,
        "valuation": valuation,
        "locality": locality,
        "locality_stats": locality_stats,
        "comparables": tagged_comparables,
        "comparable_street": street_name,
        "geo_context": geo,
        "building_breakdown": breakdown,
    }


@app.route("/")
def index():
    return render_template("index.html")


def resolve_and_report(q, source_label, lat=None, lon=None):
    match, county, raw_matches = search_address(q)
    if not match:
        return {
            "error": "no candidates found",
            "county_detected": county,
            "ppr_coverage": PPR_COVERAGE,
        }, 404

    if match["score"] < MIN_MATCH_SCORE:
        return {
            "error": f"no confident match (best candidate scored {match['score']:.0f}/100, "
                     f"below the {MIN_MATCH_SCORE} floor)",
            "county_detected": county,
            "closest_candidate": match["canonical"],
            "ppr_coverage": PPR_COVERAGE,
        }, 404

    # GPS-based callers already know where the device is pointing and pass
    # lat/lon straight through. Typed searches don't - forward-geocode the
    # *resolved* address so the geometry-based lookups (zoning, planning,
    # flood, transit, map) work from typed search too.
    #
    # Built as "house_no street, locality, Ireland" rather than the full
    # canonical string - found live that Nominatim's free-text search
    # reliably fails once a sub-locality sits between the house/street and
    # the postal district ("75 Brighton Road, Rathgar, Dublin 6" -> no
    # results) but succeeds with it dropped ("75 Brighton Road, Dublin 6"
    # -> real match). The sub-locality is exactly what search_address()
    # needs for its own matching, so it can't just be left out of
    # `canonical` - it has to be stripped only for this geocoding query.
    if lat is None or lon is None:
        try:
            conn = get_db()
            geo_row = conn.execute(
                """
                SELECT r.address, n.house_no, n.locality
                FROM ppr_normalised n JOIN ppr_raw r ON r.id = n.ppr_id
                WHERE n.county = ? AND n.canonical = ? LIMIT 1
                """,
                (county, match["canonical"]),
            ).fetchone()
            conn.close()

            geocode_query = f"{match['canonical']}, {county}" if county else match["canonical"]
            if geo_row and geo_row["house_no"] and geo_row["locality"]:
                street = extract_street_name(geo_row["address"], county)
                if street:
                    geocode_query = f"{geo_row['house_no']} {street}, {geo_row['locality']}"

            lat, lon = forward_geocode(geocode_query)
        except Exception:
            lat, lon = None, None

    report = property_report(match["canonical"], county, lat=lat, lon=lon)

    # Daft has no free-text address search URL and no open API (the real
    # one is key-gated, per-account) - this is a Google search scoped to
    # their domain, not a confirmed listing match. One click to check,
    # not a claim that the property is actually listed.
    display_address = report["history"][-1]["address"] if report["history"] else match["canonical"]
    daft_search_url = "https://www.google.com/search?q=" + urllib.parse.quote(
        f'"{display_address}" site:daft.ie'
    )

    return {
        "query": q,
        "source": source_label,
        "county_detected": county,
        "match_score": match["score"],
        "house_confirmed": match.get("house_confirmed", False),
        "resolved_canonical": match["canonical"],
        "sale_history": report["history"],
        "n_sales": len(report["history"]),
        "valuation": report["valuation"],
        "locality": report["locality"],
        "locality_stats": report["locality_stats"],
        "comparables": report["comparables"],
        "comparable_street": report["comparable_street"],
        "geo_context": report["geo_context"],
        "building_breakdown": report["building_breakdown"],
        "lat": lat,
        "lon": lon,
        "daft_search_url": daft_search_url,
        "ppr_coverage": PPR_COVERAGE,
    }, 200


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "empty query"}), 400

    body, status = resolve_and_report(q, source_label="typed")
    return jsonify(body), status


@app.route("/api/nearby")
def api_nearby():
    """
    GPS-based lookup: given lat/lon (from the browser's Geolocation API,
    i.e. a real device's real position) and an optional compass heading
    (from the browser's DeviceOrientation API), identify an address and
    run it through the same matcher as a typed search.

    With a heading, this first tries find_building_along_bearing() - a
    real ray-cast against actual OSM building polygons, not a guess.
    When that finds a building with usable address tags, its own
    addr:housenumber/addr:street are used directly, and county comes
    from a single reverse-geocode of the raw device position (still one
    Nominatim call, same as the no-heading case below - not an extra
    one over the previous baseline). This is a genuine geometric answer
    to "which building is this", confirmed live against real OSM data
    before being wired in.

    Whenever that fails - no heading, no OSM building found, or a hit
    building with no address tags - this falls back to the original
    method: project 20m forward along the bearing (see project_point,
    a straight-line guess, nothing more) and reverse-geocode whatever
    point that lands on. Free reverse geocoding also breaks down near
    large institutional sites regardless of which point is queried (see
    report section 12c) - that limitation still applies to the fallback
    path, just not to a successful building-footprint match.
    """
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "lat/lon required as numbers"}), 400

    heading_raw = request.args.get("heading")
    heading = None
    if heading_raw is not None:
        try:
            heading = float(heading_raw)
        except ValueError:
            return jsonify({"error": "heading must be a number in degrees"}), 400

    identification_method = "gps"
    projected_lat, projected_lon = lat, lon
    query_text = None
    county = None
    raw_display_name = None
    building_match = None

    if heading is not None:
        tags, dist, hit_lat, hit_lon = find_building_along_bearing(lat, lon, heading)
        if tags:
            try:
                county = reverse_geocode(lat, lon)["county"]
            except Exception:
                county = None
            # OSM's own addr tags are just house number + street, with no
            # sub-locality (Rathgar, Ranelagh, etc) - PPR addresses always
            # include one, and search_address()'s house-number-confirmed
            # path needs both to match confidently, not just street name.
            # Looked up from PPR itself (see guess_locality_for_street),
            # not Nominatim - more reliable and self-consistent.
            locality = guess_locality_for_street(
                county, tags["addr:housenumber"], tags["addr:street"], hit_lat, hit_lon
            )
            query_text = f"{tags['addr:housenumber']} {tags['addr:street']}"
            if locality:
                query_text += f", {locality}"
            projected_lat, projected_lon = hit_lat, hit_lon
            identification_method = "building_footprint"
            building_match = {
                "distance_m": round(dist, 1),
                "osm_tags": {k: v for k, v in tags.items() if k.startswith("addr:") or k == "building"},
            }

    if query_text is None:
        if heading is not None:
            projected_lat, projected_lon = project_point(lat, lon, heading)
            identification_method = "gps+compass"
        else:
            projected_lat, projected_lon = lat, lon
            identification_method = "gps"
        try:
            geo = reverse_geocode(projected_lat, projected_lon)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return jsonify({
                    "error": "Location lookup is busy right now (rate limited) - wait a "
                             "few seconds and try again.",
                }), 502
            return jsonify({"error": f"reverse geocoding failed: {e}"}), 502
        except Exception as e:
            return jsonify({"error": f"reverse geocoding failed: {e}"}), 502
        if not geo["query_text"]:
            return jsonify({
                "error": "reverse geocode returned no usable address",
                "raw_display_name": geo["raw_display_name"],
            }), 404
        query_text = geo["query_text"]
        county = geo["county"]
        raw_display_name = geo["raw_display_name"]

    q = f"{query_text}, {county}" if county else query_text
    body, status = resolve_and_report(
        q, source_label=identification_method,
        lat=projected_lat, lon=projected_lon,
    )
    body["reverse_geocoded_address"] = query_text
    body["raw_osm_display_name"] = raw_display_name
    body["device_position"] = {"lat": lat, "lon": lon}
    body["heading_deg"] = heading
    body["geocoded_position"] = {"lat": projected_lat, "lon": projected_lon}
    body["identification_method"] = identification_method
    body["building_match"] = building_match
    return jsonify(body), status


if __name__ == "__main__":
    # RENDER=true is set specifically by Render's platform, unlike PORT
    # which turned out to also be set by the local dev preview tool this
    # was built with - checking for PORT alone silently dropped HTTPS
    # during local testing, found by testing rather than assuming. On
    # Render, the platform's own reverse proxy terminates HTTPS, so no
    # self-signed cert is needed there. Locally, keep using one, since
    # mobile browsers only expose the Geolocation API on a secure context
    # and a plain LAN IP over HTTP doesn't qualify - confirmed live on
    # iOS Safari: "Origin does not have permission to use Geolocation
    # service".
    port = int(os.environ.get("PORT", 5057))
    if os.environ.get("RENDER") == "true":
        app.run(debug=False, host="0.0.0.0", port=port)
    else:
        cert_path = ROOT / "certs" / "cert.pem"
        key_path = ROOT / "certs" / "key.pem"
        ssl_context = (str(cert_path), str(key_path)) if cert_path.exists() else None
        app.run(debug=False, host="0.0.0.0", port=port, ssl_context=ssl_context)
