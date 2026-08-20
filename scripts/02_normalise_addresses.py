"""
Phase 2 (finally): PPR address normaliser.

Turns free-text PPR addresses into a canonical matching string plus a
few extracted components (house number, locality, embedded Eircode).
This is what the BER spike never needed to build - PPR has addresses,
BER doesn't, so this only has to solve PPR-to-PPR and query-to-PPR
matching, not PPR-to-BER.
"""
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "spike.db"

EIRCODE_RE = re.compile(r'\b([AC-FHKNPRTV-Y]\d{2})\s?([AC-FHKNPRTV-Y0-9]{4})\b', re.IGNORECASE)

# order matters: longer/more specific patterns first
ABBREVIATIONS = [
    (r'\broad\b', 'rd'),
    (r'\bavenue\b', 'ave'),
    (r'\bavenue\b', 'ave'),
    (r'\bdrive\b', 'dr'),
    (r'\bcrescent\b', 'cres'),
    (r'\bterrace\b', 'tce'),
    (r'\bpark\b', 'pk'),
    (r'\bheights\b', 'hts'),
    (r'\bcourt\b', 'ct'),
    (r'\bapartment\b', 'apt'),
    (r'\bapartments\b', 'apt'),
    (r'\bapts\b', 'apt'),
    (r'\bhouse\b', 'ho'),
    (r'\bclose\b', 'cl'),
    (r'\bplace\b', 'pl'),
    (r'\bgrove\b', 'gr'),
    (r'\blane\b', 'ln'),
    (r'\bmanor\b', 'mnr'),
    (r'\bcottages\b', 'cotts'),
    (r'\bestate\b', 'est'),
    (r'\bview\b', 'vw'),
    (r'\bwalk\b', 'wlk'),
]

COUNTY_PREFIX_RE = re.compile(r'^\s*(?:co\.\s*|co\s+|county\s+)', re.IGNORECASE)

IRISH_ENGLISH_EQUIV = {
    "baile atha cliath": "dublin",
    "cathair chorcai": "cork city",
    "corcaigh": "cork",
    "gaillimh": "galway",
    "luimneach": "limerick",
    "port lairge": "waterford",
}


def lower_and_tidy(s):
    # lowercase and collapse whitespace, but keep commas intact -
    # county-stripping and locality extraction both split on commas,
    # so punctuation has to survive until after that happens.
    s = s.strip().lower()
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\s*,\s*', ', ', s)
    return s.strip()


def fold(s):
    """ASCII-fold for fuzzy matching (Sli -> sli, Cóbh -> cobh)."""
    import unicodedata
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def extract_eircode(text):
    m = EIRCODE_RE.search(text)
    if not m:
        return text, None
    code = (m.group(1) + m.group(2)).upper()
    cleaned = text[:m.start()] + text[m.end():]
    return cleaned, code


def extract_house_no(text):
    text = re.sub(r'^\s*no\.?\s*', '', text, flags=re.IGNORECASE)
    m = re.match(r'^\s*(\d+[a-zA-Z]?)\b', text)
    if m:
        return m.group(1).lower(), text[m.end():].strip()
    return None, text


COUNTRY_TOKENS = {"ireland", "eire", "republic of ireland", "rep of ireland", "roi"}


def strip_county_suffix(text, county):
    """
    Strip trailing noise segments: country name, then county name,
    repeated for as many redundant trailing segments as there are.

    A real address that reached this (from a manually pasted map
    location, not just PPR's own data) was "...Dublin 6, Dublin,
    Ireland" - two redundant segments stacked after the real locality.
    A single last-segment check only strips one of them and leaves
    "Ireland" as the extracted locality, which is worse than useless
    for matching. Loop until nothing more matches.
    """
    county_fold = fold(county.lower()) if county else None
    parts = [p.strip() for p in text.split(',')]
    while parts:
        last_fold = fold(COUNTY_PREFIX_RE.sub('', parts[-1]).strip())
        if last_fold in COUNTRY_TOKENS or (county_fold and last_fold == county_fold):
            parts = parts[:-1]
        else:
            break
    # "Dublin N" postal districts are kept - they are locality signal,
    # not noise, unlike bare county/country repeats
    return ', '.join(parts)


def expand_abbreviations(text):
    for pattern, repl in ABBREVIATIONS:
        text = re.sub(pattern, repl, text)
    return text


def normalise_address(raw_address, county):
    working = raw_address
    working, eircode = extract_eircode(working)
    working = lower_and_tidy(working)
    working = strip_county_suffix(working, county)
    house_no, remainder = extract_house_no(working)
    remainder = expand_abbreviations(remainder)
    remainder = re.sub(r'\s+', ' ', remainder).strip(' ,')

    tokens = [t.strip() for t in remainder.split(',') if t.strip()]
    locality = None
    if tokens:
        locality = fold(tokens[-1])
        locality = re.sub(r'[^a-z0-9 ]', '', locality).strip()

    canonical_parts = ([house_no] if house_no else []) + tokens
    canonical = fold(' '.join(canonical_parts))
    canonical = re.sub(r'[^a-z0-9 ]', '', canonical)
    canonical = re.sub(r'\s+', ' ', canonical).strip()

    return {
        "canonical": canonical,
        "house_no": house_no,
        "locality": fold(locality) if locality else None,
        "eircode": eircode,
    }


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS ppr_normalised")
    cur.execute("""
    CREATE TABLE ppr_normalised (
        ppr_id INTEGER PRIMARY KEY,
        canonical TEXT,
        house_no TEXT,
        locality TEXT,
        eircode_extracted TEXT,
        county TEXT
    )
    """)

    rows = cur.execute("SELECT id, address, county, eircode FROM ppr_raw").fetchall()
    out = []
    for ppr_id, address, county, existing_eircode in rows:
        n = normalise_address(address, county)
        eircode = existing_eircode.strip() if existing_eircode and existing_eircode.strip() else n["eircode"]
        out.append((ppr_id, n["canonical"], n["house_no"], n["locality"], eircode, county))

    cur.executemany(
        "INSERT INTO ppr_normalised VALUES (?,?,?,?,?,?)", out
    )
    conn.commit()

    cur.execute("CREATE INDEX idx_norm_canonical ON ppr_normalised(canonical)")
    cur.execute("CREATE INDEX idx_norm_county ON ppr_normalised(county)")
    cur.execute("CREATE INDEX idx_norm_locality ON ppr_normalised(locality)")
    conn.commit()

    print(f"Normalised {len(out)} PPR addresses.")
    print("\nSample:")
    for ppr_id, canonical, house_no, locality, eircode, county in out[:10]:
        print(f"  {canonical!r:55} house_no={house_no!r} locality={locality!r} eircode={eircode!r}")

    conn.close()
