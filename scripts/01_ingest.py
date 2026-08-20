"""
Phase 1: ingest raw PPR and BER extracts into SQLite, unmodified.
No normalisation here. This just gets the data into a queryable form
and lets us measure what we are actually working with.
"""
import csv
import sqlite3
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "spike.db"
PPR_CSV = ROOT / "data" / "raw" / "ppr_extract" / "PPR-ALL.csv"
BER_TXT = ROOT / "data" / "raw" / "ber_extract" / "BERPublicsearch.txt"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
if DB_PATH.exists():
    DB_PATH.unlink()

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL")
cur = conn.cursor()

# ---------------------------------------------------------------------------
# PPR
# ---------------------------------------------------------------------------
cur.execute("""
CREATE TABLE ppr_raw (
    id INTEGER PRIMARY KEY,
    date_of_sale TEXT,
    address TEXT,
    county TEXT,
    eircode TEXT,
    price_raw TEXT,
    price_eur REAL,
    not_full_market_price TEXT,
    vat_exclusive TEXT,
    description TEXT,
    size_description TEXT
)
""")

def clean_price(raw):
    # strips currency symbol (decoded euro sign) and thousands separators
    digits = raw.strip().lstrip("€").replace(",", "")
    try:
        return float(digits)
    except ValueError:
        return None

with open(PPR_CSV, encoding="cp1252", newline="") as f:
    reader = csv.reader(f)
    header = next(reader)
    rows = (
        (
            r[0], r[1], r[2], r[3], r[4], clean_price(r[4]),
            r[5], r[6], r[7], r[8],
        )
        for r in reader
    )
    cur.executemany(
        """INSERT INTO ppr_raw
           (date_of_sale, address, county, eircode, price_raw, price_eur,
            not_full_market_price, vat_exclusive, description, size_description)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )

conn.commit()
ppr_count = cur.execute("SELECT COUNT(*) FROM ppr_raw").fetchone()[0]
print(f"PPR rows loaded: {ppr_count}")

# ---------------------------------------------------------------------------
# BER — tab-delimited, ~252 columns. We keep the columns relevant to a
# potential match plus a handful of dwelling attributes for later profiling.
# Encoding: BER file declared/observed as UTF-8 with some Windows artefacts;
# we decode leniently and replace undecodable bytes rather than fail the load.
# ---------------------------------------------------------------------------
cur.execute("""
CREATE TABLE ber_raw (
    id INTEGER PRIMARY KEY,
    county_name TEXT,
    dwelling_type TEXT,
    year_of_construction TEXT,
    type_of_rating TEXT,
    energy_rating TEXT,
    ber_rating TEXT,
    ground_floor_area TEXT,
    sa_code TEXT,
    date_of_assessment TEXT,
    purpose_of_rating TEXT
)
""")

def strip_nul(fileobj):
    for line in fileobj:
        if "\x00" in line:
            line = line.replace("\x00", "")
        yield line

with open(BER_TXT, encoding="utf-8", errors="replace", newline="") as f:
    reader = csv.reader(strip_nul(f), delimiter="\t")
    header = next(reader)
    idx = {name: i for i, name in enumerate(header)}

    def col(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return None
        return row[i]

    def gen():
        for r in reader:
            yield (
                col(r, "CountyName"),
                col(r, "DwellingTypeDescr"),
                col(r, "Year_of_Construction"),
                col(r, "TypeofRating"),
                col(r, "EnergyRating"),
                col(r, "BerRating"),
                col(r, "GroundFloorArea(sq m)"),
                col(r, "SA_Code"),
                col(r, "DateOfAssessment"),
                col(r, "PurposeOfRating"),
            )

    cur.executemany(
        """INSERT INTO ber_raw
           (county_name, dwelling_type, year_of_construction, type_of_rating,
            energy_rating, ber_rating, ground_floor_area, sa_code,
            date_of_assessment, purpose_of_rating)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        gen(),
    )

conn.commit()
ber_count = cur.execute("SELECT COUNT(*) FROM ber_raw").fetchone()[0]
print(f"BER rows loaded: {ber_count}")
print(f"BER columns available in source (kept a subset): {len(header)}")

# save full header list for the record
with open(ROOT / "data" / "processed" / "ber_full_header.txt", "w") as f:
    f.write("\n".join(header))

conn.execute("CREATE INDEX idx_ppr_county ON ppr_raw(county)")
conn.execute("CREATE INDEX idx_ber_county ON ber_raw(county_name)")
conn.commit()
conn.close()
print("Done.")
