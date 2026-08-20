"""
Local demo service: type an address (stand-in for a camera scan),
get back the resolved property's sale history plus nearby comparables,
built entirely from PPR. No BER data involved - that piece is still
blocked pending the SEAI/CSO requests.
"""
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import sys
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
    <!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:320px;margin:80px auto">
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
    return {
        "estimated_value": round(last_price * ratio, 2),
        "based_on_sale_year": last_sale_year,
        "index_latest_year": latest_year,
        "county_price_change_pct": round((ratio - 1) * 100, 1),
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
    req = urllib.request.Request(url, headers={"User-Agent": NOMINATIM_UA})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))

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
        "raw_display_name": data.get("display_name"),
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


def property_report(canonical, county):
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

    comparables = []
    if locality:
        comparables = conn.execute(
            f"""
            SELECT r.date_of_sale, r.price_eur, r.address
            FROM ppr_normalised n JOIN ppr_raw r ON r.id = n.ppr_id
            WHERE n.county = ? AND n.locality = ? AND n.canonical <> ?
            ORDER BY {DATE_SORT_SQL} DESC
            LIMIT 10
            """,
            (county, locality, canonical),
        ).fetchall()

    conn.close()

    def tag(row):
        d = dict(row)
        d["is_bulk_sale"] = is_bulk_sale(d["address"])
        return d

    tagged_history = [tag(h) for h in history]
    valuation = None
    if tagged_history:
        latest_sale = tagged_history[-1]
        if not latest_sale["is_bulk_sale"]:
            sale_year = latest_sale["date_of_sale"][-4:]
            valuation = estimate_current_value(county, latest_sale["price_eur"], sale_year)

    return {
        "history": tagged_history,
        "valuation": valuation,
        "locality": locality,
        "comparables": [tag(c) for c in comparables],
    }


@app.route("/")
def index():
    return render_template("index.html")


def resolve_and_report(q, source_label):
    match, county, raw_matches = search_address(q)
    if not match:
        return {"error": "no candidates found", "county_detected": county}, 404

    if match["score"] < MIN_MATCH_SCORE:
        return {
            "error": f"no confident match (best candidate scored {match['score']:.0f}/100, "
                     f"below the {MIN_MATCH_SCORE} floor)",
            "county_detected": county,
            "closest_candidate": match["canonical"],
        }, 404

    report = property_report(match["canonical"], county)
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
        "comparables": report["comparables"],
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
    (from the browser's DeviceOrientation API), reverse-geocode to a
    street address and run it through the same matcher as a typed
    search.

    With a heading, this projects 20m forward along that bearing first
    (see project_point) - an approximation of "what the camera is
    facing" rather than "where the phone is standing". Without one, it
    falls back to reverse-geocoding the raw position, same as before.

    Neither mode knows which specific building is in frame - that needs
    a building-footprint dataset (Eircode ECAD, GeoDirectory, OSi),
    which this spike has not licensed. Confirmed live and unfixable by
    this projection alone: free reverse geocoding also breaks down near
    large institutional sites regardless of which point is queried (see
    report section 12c) - the projection changes which point is asked
    about, not how precise the underlying map data is.
    """
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "lat/lon required as numbers"}), 400

    heading_raw = request.args.get("heading")
    heading = None
    projected_lat, projected_lon = lat, lon
    if heading_raw is not None:
        try:
            heading = float(heading_raw)
        except ValueError:
            return jsonify({"error": "heading must be a number in degrees"}), 400
        projected_lat, projected_lon = project_point(lat, lon, heading)

    try:
        geo = reverse_geocode(projected_lat, projected_lon)
    except Exception as e:
        return jsonify({"error": f"reverse geocoding failed: {e}"}), 502

    if not geo["query_text"]:
        return jsonify({
            "error": "reverse geocode returned no usable address",
            "raw_display_name": geo["raw_display_name"],
        }), 404

    q = f"{geo['query_text']}, {geo['county']}" if geo["county"] else geo["query_text"]
    body, status = resolve_and_report(q, source_label="gps+compass" if heading is not None else "gps")
    body["reverse_geocoded_address"] = geo["query_text"]
    body["raw_osm_display_name"] = geo["raw_display_name"]
    body["device_position"] = {"lat": lat, "lon": lon}
    body["heading_deg"] = heading
    body["geocoded_position"] = {"lat": projected_lat, "lon": projected_lon}
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
