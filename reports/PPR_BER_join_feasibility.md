# PPR to BER address-level join: feasibility spike

Date: 2026-08-19. Updated 2026-08-20 with sections 11-15 below, covering
a PPR-only address engine and camera-feature groundwork built and
live-tested after the original spike concluded.

Scope: sections 1-10 are the original go/no-go spike (technical only,
no product built). Sections 11 onward cover what was built afterward
once BER was ruled out as a near-term blocker and the question shifted
to "what can be built on PPR alone."

## Recommendation, original scope: NO-GO on the free, no-licence path
## for the PPR-to-BER join. Do not build a matching pipeline against
## the public SEAI BER extract.

The blocker is not address quality, and it is not solvable with a
better normaliser, more matching passes, or an Eircode licence. The
public BER dataset does not contain the information needed to
identify which dwelling a record refers to below county level. This
was proven directly, not inferred.

A real path to the BER side of the product exists, but it runs
through a formal data relationship with SEAI or the CSO, not through
engineering. See "What to request" below - both requests have now
been sent, see section 14 for status.

**Updated picture as of section 15: a PPR-only product (price history,
repeat-sale detection, comparables, resolved from a typed address or a
real device's GPS) is proven buildable and has been live-tested
end-to-end on a real phone. The BER/energy-rating layer remains
blocked exactly as below. A true camera/AR building-identification
layer needs a licensed building-footprint dataset - free point-based
reverse geocoding was tested live and found unreliable near
institutional campuses (section 12c).**

---

## 1. What was tested

- PPR: full CSV export, all counties, 2010 to date. 801,320 rows.
  Downloaded and ingested successfully; encoding is Windows-1252
  (cp1252), not strict Latin-1 - noted because a strict Latin-1
  decode silently corrupts the euro sign and would have looked like
  a successful load.
- BER: full bulk export from the SEAI BER Research Tool. 1,399,937
  rows across 252 published columns. The "Download All Data" control
  is an ASP.NET form postback rather than a static link; it was
  automated successfully and did not require a manual download.
- Both loaded into SQLite (`data/processed/spike.db`, tables
  `ppr_raw` and `ber_raw`) for direct, reproducible querying.

## 2. The finding: BER has no address, no Eircode, and effectively no
## sub-county geography

Checked against all 252 columns in the source file, not just the
ones loaded into SQLite. Confirmed absent:

- No address field of any kind.
- No Eircode field.
- `SA_Code` (Census Small Area, the next geographic level down from
  county, normally covering 50-200 dwellings) exists as a column but
  is populated in 2 rows out of 1,399,937. Not usable.

The only geography in the file is `CountyName`: 26 counties, except
Dublin is broken out by postal district (`Dublin 1`...`Dublin 24`,
`Dublin 6W`) instead of given as one county, and Cork, Galway,
Limerick and Waterford separate "City" from "Co.". 55 distinct
values in total.

This is a deliberate anonymisation choice by SEAI, not a data-entry
gap. A BER certificate itself does carry the property address (it
has to, an assessor needs to find the building), and SEAI's own
regulations (S.I. No. 243/2012) explicitly name who is entitled to
the full record - the assessor, a subsequent assessor, or the
building owner or their agent. The public bulk export is the
statistically-anonymised subset everyone else gets.

**Why an Eircode lookup API does not help:** such an API takes an
address or coordinates as input and returns an Eircode as output. BER
rows contain neither, so there is nothing to feed it. This point is
worth stating plainly because it is the natural next idea and it does
not survive contact with what is actually in the file.

## 3. Quantifying the wall, not just asserting it

Two measurements, both against real data, not simulated.

**3a. BER-side uniqueness ceiling.** Bucketed all BER rows by
(county, dwelling type, year of construction, floor area rounded to
the nearest 10 sqm) - the finest non-address signal the file offers,
before any attempt to also line each one up with a specific PPR sale.

- 110,322 of 1,399,937 BER rows (7.9%) are unique on that combination
  within their own county.
- The remainder share their bucket with at least one, often ten or
  more, other BER rows in the same county.

This is a ceiling on the BER side alone. It is not achievable in
practice, only an upper bound.

**3b. Actual coarse join, best-case subset.** PPR's own fields are
too coarse to use most of what BER offers anyway: PPR has no floor
area for second-hand sales (93.4% of all sales) and only a wide band
for new builds; PPR's "description" field distinguishes new vs
second-hand but not dwelling type (semi-detached, terrace, apartment,
etc.) the way BER does. The only subset where PPR's sale date and
BER's assessment date should plausibly line up is new-build sales
matched to BER's "new dwelling for owner occupation" category, joined
on county and year - the most forgiving version of this join
possible.

Result, run against the full data:

- 143,265 PPR new-build sales in the subset.
- Joined to 70,538 BER "new dwelling for owner occupation" rows on
  (county, year).
- 3.9% have zero same-county-and-year BER candidates.
- **1.7% resolve to exactly one candidate (a genuine unique match).**
- 94.4% have more than one candidate, with a median of 350 competing
  BER records and a maximum of 4,492.

This is the real, measured number for the single most favourable
subset available. Second-hand sales (82.1% of PPR) would score worse
again, because sale date has no defined relationship to either BER's
construction year or assessment date for an existing dwelling.

## 4. Gate result

| Threshold | Result |
|---|---|
| >=85% GO | Not met. Not close. |
| 70-85% marginal | Not met. |
| <70% NO-GO | **Met.** Best-case measured rate: 1.7%. Realistic overall rate: lower. |

## 5. Failure taxonomy

Ordinarily this section would break down failure by cause across
strategies (exact canonical, Eircode, fuzzy token-set, trigram) as
scoped in the brief. That breakdown does not apply here, because none
of those strategies were run - phase 3 was not built. There is a
single root cause, and it accounts for the entire gap: **BER carries
no address-resolvable field**. Every downstream failure mode
(ambiguous candidates, unresolved sales, false positives from
accepting a plausible-looking but wrong candidate) is a direct
consequence of that one fact, not an independent problem to solve
separately.

## 6. False positive rate

Not measured by hand-sampling 100 matches, because there is no
responsible "best guess" strategy to sample from. With a median of
350 candidates competing for each PPR new-build sale under the most
forgiving join tested, any rule that force-picks a single winner
(nearest price rank, first record, etc.) would be close to a random
draw dressed up as a match. Reporting a false positive rate on that
would imply a level of rigour the underlying data does not support.

## 7. PPR data quality (for the record - not the blocker)

None of this is why the project fails, but it is useful context if a
future address-holding BER extract does materialise:

- Eircode column present and 30.7% filled (245,697 / 801,320 rows),
  99.4% valid format where present. The brief's assumption that PPR
  has no Eircode column at all is out of date; the column exists, it
  is just sparse.
- Eircode-shaped substrings embedded in address text where the
  column is empty: 46 rows out of 555,623 (0.01%) - real, but not a
  meaningful recovery source.
- 1,032 exact duplicate rows (same address, date, price).
- 1,711 rows (0.2%) contain genuine Irish-language place names,
  correctly decoded - not an encoding defect.
- One row has a corrupted description field
  ("Teach/?ras?n C?naithe Nua") - a pre-existing defect in PPR's own
  export pipeline, immaterial at this volume.
- size_description (a floor-area band) is only populated for new
  builds, 6.6% of all rows.

## 8. Estimated uplift from an Eircode licence

Purchasing an ECAF/ECAD licence (roughly EUR 60-180 plus a
per-transaction fee, per Eircode's published pricing) would let you
geocode PPR's free-text addresses to a validated Eircode with high
confidence for close to 100% of rows, up from the 30.7% that already
self-report one. That is a real, useful capability.

**It moves the match rate by approximately 0%.** The bottleneck is
not PPR's address quality, it is that BER has nothing to match a
resolved Eircode against. An Eircode licence fixes the side of the
join that was never the constraint. Do not buy one for this purpose;
it would be a real cost solving a problem this project does not have.
It may still be worth having independently for other PPR-side work
(deduplication, geocoding for mapping), but that is a separate
decision from this gate.

## 9. What to request, if you want to pursue this differently

Two real, sourced options exist. Both are administrative/legal, not
technical, and neither is something this spike can execute.

**a. SEAI direct data request.** SEAI takes data requests for BER
information beyond the public extract. Regulation S.I. No. 243/2012
governs who is entitled to full-record (address-level) access:
the assessor who carried out the BER, a subsequent assessor, or the
building owner or their agent. A commercial research request outside
those categories would need to go through SEAI's own review process,
which can approve, reject, or ask for clarification - approval is not
guaranteed, and there is likely to be a fee and a data-sharing
agreement involved for any use beyond narrow research purposes. This
is the direct route to ask "can we license an address- or
Eircode-linked BER extract for a commercial product."

**b. CSO Research Microdata Files.** This is the more interesting
finding: the Central Statistics Office already performs this exact
linkage internally. Their Residential Property Price Index and
related publications match PPR-style address and price data to SEAI
BER data, using pseudonymised Protected Identifier Keys under Section
30 of the Statistics Act 1993 - a statutory power only the CSO holds,
which is why they can do what this spike cannot. The linked output is
available to accredited researchers as Research Microdata Files,
subject to the CSO's access policy. Two caveats worth checking before
counting on this: RMF access is typically framed around bona fide
statistical research, and it is not yet confirmed whether the terms
would permit use in a commercial product versus research-only
analysis - that needs a direct conversation with CSO, not an
assumption either way. It is also worth checking the CSO's own
published outputs first (Residential Property Price Index background
notes, "Characteristics of Residential Property Purchasers" reports)
in case some of what the product wants already exists as free,
published aggregate statistics.

Neither of these is a weekend's work. Both are worth a phone call
before deciding this is dead, because they are the only routes that
plausibly clear the 85% bar - no amount of further engineering on the
public files will.

## 10. What was not built, and why

Phases 2 (address normaliser), 3 (matching pipeline) and 4 (full
measurement with hand-sampled false positives) were not built as part
of the original BER-join spike. The brief's own constraint - "do not
optimise anything that does not move the match rate" - applied
directly: none of that work would have moved the BER match rate,
because the ceiling was fixed by BER's contents before any of it
started.

The address normaliser was subsequently built anyway, once the
question changed from "can we join to BER" to "what can we build on
PPR alone." That work, and what it found, is below.

---

## 11. What was built: a PPR-only address engine

Scope changed after a demo of an Australian app (Glasshouse - "point
your phone at any property, know everything") raised the question of
whether the camera/lookup half of that product could be built for
Ireland on PPR alone, independent of the BER question. PPR has no
anonymisation wall - the blocker there is data quality and matching
precision, an engineering problem rather than a data-access one - so
this was worth actually building and testing, not just estimating.

Built:

- **Address normaliser** (`scripts/02_normalise_addresses.py`) -
  strips county/country duplication, expands abbreviations, extracts
  embedded Eircodes, ASCII-folds Irish-language place names, and
  splits every PPR address into house number, street, and locality.
  Re-run over all 801,320 PPR rows.
- **Repeat-sale detection** - groups PPR rows by canonical address to
  build a price-history timeline per property. Naively this looked
  like 28.8% of PPR rows had a repeat sale; restricting to addresses
  with a parsed house number (excluding estate names with no number,
  which were collapsing dozens of unrelated houses into one fake
  "property") brought this down to a real, checked 16.9%.
- **Two-stage fuzzy matcher** (`app/server.py`) - resolves a typed or
  GPS-derived address to a specific PPR record: fuzzy-match the
  locality first, then require an exact house-number match within it,
  with a street-name similarity guard before trusting that match (see
  section 12 for why the guard exists). Falls back to a whole-string
  fuzzy match, explicitly flagged as unconfirmed, when no confirmed
  house number is found.
- **GPS lookup** (`/api/nearby`) - takes a lat/lon from the browser's
  real Geolocation API, reverse-geocodes it via OpenStreetMap's free
  Nominatim service (single on-demand lookups only, per Nominatim's
  usage policy), and feeds the result through the same matcher.
- **Bulk/institutional sale tagging** - flags sale records that are a
  whole building sold to one landlord (e.g. "comprising Block F and
  Block G") rather than a single dwelling, so they are not presented
  as an ordinary comparable. Calibrated against real PPR data (299
  matches nationally, spot-checked clean from a 4-cottage sale up to
  a EUR217m, 461-apartment block).
- **Local web front end**, run over HTTPS with a self-signed cert on
  the LAN so a real phone could test the real Geolocation API - plain
  HTTP does not qualify as a secure context on iOS Safari, confirmed
  live ("Origin does not have permission to use Geolocation service").

## 12. Real-device testing: three findings, not zero

All of this was tested against real PPR data from a desktop browser
first, then handed to a real iPhone on the same Wi-Fi. The phone test
found two genuine bugs and one structural limitation that desktop
testing had missed entirely.

**12a. Locality-collision false confidence.** A GPS point near
Dundrum reverse-geocoded to "62 Churchfields." The matcher confidently
(100%, "house number confirmed") returned the sale history for house
62 on a *different* street, "Beech Lawn," because both streets sit in
the same coarse locality bucket ("Dundrum") and the exact-house-number
check did not verify the street name matched too. Fixed by comparing
street name similarity (with locality stripped from both sides, see
12b) before trusting an exact house-number hit, requiring at least 55
similarity. A second live test then caught a related case - "59
Orwell Road" resolving to "59 Rathgar Road" at 73/100, passing an
earlier, cruder guard, because "Rathgar" is both the locality and part
of the wrong street's own name, double-counting in a whole-string
comparison. Fixed by stripping the locality from both sides before
comparing street names specifically.

**12b. Trailing suffix duplication corrupting locality extraction.** A
manually-entered query "1 Wesley Road, Rathgar, Dublin 6, Dublin,
Ireland" has two redundant trailing segments after the real locality
("Dublin 6, Dublin, Ireland"). The original county-stripping logic
only ever checked the single last segment, so it stripped nothing
useful and extracted `locality: "ireland"` - garbage. This made a
genuinely correct, exact match (a real EUR722,000 sale at that exact
address) get reported as an unconfirmed low-confidence guess. Fixed
by looping the strip and recognising "Ireland"/"Eire" as a country
token to remove, not just a county. Re-ran the normaliser over all
801,320 PPR rows afterward, since this changes how every row is
parsed, not just live queries. No regressions on the earlier test
cases.

**12c. Free reverse geocoding is unreliable near institutional
campuses.** Tested from a real position (a rented apartment block at
75 Orwell Road, Rathgar, confirmed against Google Maps). Nominatim
resolved the exact coordinate to "Lucena Clinic & St Peter's School,
59 Orwell Road" - a hospital/clinic/school site, not the actual
building. OpenStreetMap has that entire institutional campus mapped
as one large polygon (bounding box roughly 200m x 350m) with a single
address point attached to the whole site, so anyone standing anywhere
in that area gets the same wrong answer regardless of which specific
building they are actually in. This is not a GPS accuracy problem -
the phone's own position was correct - and it is not something the
PPR matcher can fix, because the input address it receives is already
wrong before matching starts. It confirms the point made when GPS work
began: a real camera/AR building-identification feature needs a
licensed building-footprint dataset with individual polygons per
building (Eircode's ECAD, GeoDirectory, or OSi's building layer), not
free point-based reverse geocoding against crowdsourced OSM data. That
is a paid-data problem, the same category of blocker as BER, just on
the geocoding side rather than the energy-rating side.

## 13. A live example of the bulk-sale blind spot

The same real address used for 12c turned out to be a genuinely useful
test case for its own reasons: 75 Orwell Road is a purpose-built
rental block. It exists in PPR, but only as a single record - "The
Gate Lodge and Rostrevor Place, (comprising Block F and Block G), 75
Orwell Road, Rathgar" - sold whole to an institutional landlord on
17/09/2021 for EUR54,587,444.93. There is no per-unit sale history for
this building in PPR, because it has never been sold unit by unit.

This is the real-world confirmation of what the bulk-sale tagging in
section 11 is for: purpose-built rental buildings, an increasingly
common typology in Dublin, are a structural blind spot for any
PPR-based per-property price history, not a matching failure. The
system now tags this correctly rather than silently having nothing to
show, or worse, confidently showing the wrong nearby building's price
as if it were this one.

## 14. Data request status

Both requests from section 9 were sent (2026-08-19):

- **SEAI** (`info@ber.seai.ie`) - asked whether an address- or
  Eircode-linked BER extract could be licensed, what it would cost,
  and whether commercial/product use would be permitted. No response
  yet at time of writing.
- **CSO Research Coordination Unit** (`rcu@cso.ie`) - asked, ahead of
  the formal Research Organisation Registration process, whether a
  commercial entity is even eligible for RMF access, whether the
  intended commercial use is permitted at all, and whether a bespoke
  BER-to-transaction linkage (rather than the Census-based one they
  already publish) exists or could be produced. No response yet at
  time of writing.

Both are genuine blockers on the BER side specifically. Neither blocks
the PPR-only work in sections 11-13, which needs no external data
relationship to keep going.

## 15. Updated recommendation

- **BER join: unchanged, NO-GO** on the free path. Waiting on SEAI/CSO
  responses before there is anything new to decide here.
- **PPR-only price history and comparables: proven, not just
  estimated.** Built, tested against real data, and live-tested twice
  on a real device on a real residential street, which is what
  surfaced all three findings above. Ready to be extended rather than
  re-validated from scratch.
- **Camera/AR building identification specifically: still needs a
  licensed building-footprint or address-point dataset.** This was
  the known gap before GPS work started, and live testing confirmed it
  is the binding constraint, not an engineering shortfall - free GPS
  and free reverse geocoding are not precise enough near mixed-use or
  institutional areas, which are exactly the areas a dense-city
  product would need to work in reliably.
- The institutional bulk-sale tagging (section 13) should be treated
  as a permanent feature of any PPR-based product, not a one-off
  workaround - it was found from a single real test address and is
  very unlikely to be rare in a market with as much purpose-built
  rental stock as Dublin now has.
