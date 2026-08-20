# Phase 1 findings (data ingest and profiling)

## Sources obtained

- PPR: `PPR-ALL.zip` downloaded direct from propertypriceregister.ie.
  801,320 rows, 2010-01-01 to 2025-12-31. Encoding is Windows-1252
  (cp1252), not strict ISO-8859-1/Latin-1 - byte 0x80 decodes to the
  euro sign under cp1252 but is undefined in true Latin-1. Loaded into
  SQLite table `ppr_raw` (9 columns as published, price split into
  raw string and cleaned REAL).
- BER: `BERPublicSearch.zip` downloaded from the SEAI BER Research
  Tool. This required an ASP.NET postback (a form submit, not a
  plain link) - automated successfully, no manual download needed.
  1,399,937 rows, tab-delimited, 252 columns. Loaded a 10-column
  subset relevant to matching into `ber_raw`; full header list saved
  separately for reference.

Both downloads needed a workaround for a broken TLS chain on
propertypriceregister.ie (the server does not send its intermediate
certificate). Fixed by fetching Sectigo's intermediate and verifying
against it, not by disabling verification.

## PPR data quality

- Row count: 801,320. No nulls in date, address, county, or price.
- Eircode column: present (contrary to the brief's assumption that
  there is no Eircode field - there is one, it is just sparse).
  Filled in 245,697 rows (30.7%). Of those, 99.4% are valid Eircode
  format. So roughly a third of PPR rows already carry a usable
  Eircode; two-thirds do not.
- Eircode-shaped substrings embedded in the address text, where the
  Eircode column itself is empty: 46 rows out of 555,623 (0.01%).
  This "known problem" is real but negligible as a recovery source.
- Size Description populated in only 52,845 rows (6.6%) - it is only
  filled for new-build sales, as expected from the PPR schema.
- 1,032 rows are exact duplicates on (address, date, price).
- 1,711 rows (0.2%) contain non-ASCII characters - genuine Irish-
  language place names (fada), correctly decoded, not mojibake.
- Price cleaned to a REAL column; range is credible (min 5,001, max
  387.6m - the max is very likely a commercial/non-residential entry
  or data error, immaterial to the join).

## BER data quality - critical finding

**The public BER bulk export contains no address field and no
Eircode field, at all.** This was checked directly against all 252
published columns, not just the 10 loaded - confirmed absent.

The only geography in the file is `CountyName`, which is county-level
for 25 of the 26 counties (e.g. "Co. Clare") and gives Dublin postal
district instead of county for Dublin properties (e.g. "Dublin 14",
"Cork City" and "Dublin City" are separate categories from their
"Co." counterparts). Total distinct values: 55.

There is a `SA_Code` column (Census Small Area, the next level down,
normally ~50-200 dwellings) that would have been a workable geographic
fallback. It is populated in 2 rows out of 1,399,937. Effectively
absent.

This is a data availability wall, not an address-parsing problem.
No amount of normalisation on the PPR side can produce an
address-level join against a target table that has no address.

### Quantifying the fallback ceiling

To check whether county + dwelling attributes could stand in for
address, I bucketed BER rows by (county, dwelling type, construction
year, floor area rounded to the nearest 10 sqm) - the most granular
non-address signal the file offers - and measured how many rows land
in a bucket by themselves within their own county, before any
comparison to PPR at all:

- 110,322 of 1,399,937 BER rows (7.9%) are unique on that combination
  within their county.
- The rest share their bucket with at least one, and often ten-plus,
  other BER rows in the same county (e.g. 21,159 buckets contain
  exactly 3 rows each; some buckets are far larger).

7.9% is a ceiling, not an achievable rate - it is BER-side
uniqueness only, before attempting to also line each one up with the
correct PPR sale. The real number after joining to PPR will be lower
again. It is nowhere near the 70% marginal threshold, let alone the
85% go threshold.

## What this means for the gate

The objective as scoped - join PPR to BER at address level - cannot
be met using data obtained from the public SEAI BER Research Tool,
regardless of how good the PPR-side address normaliser is. This is
not a phase 2/3 engineering problem. The BER file simply does not
carry the information needed to identify which dwelling a record
refers to below county level.

This does not by itself answer the overall product go/no-go - it
answers a narrower and more useful question first: this specific,
free, no-licence data path is a dead end for the join. There are two
live options, both outside what phase 2/3 engineering can fix:

1. Request an address- or Eircode-linked BER extract directly from
   SEAI under a research/data-sharing agreement. SEAI is legally
   permitted to hold this data linked; it chooses to anonymise the
   public bulk file. This is an administrative/legal path, not a
   technical one, and is outside this spike's scope to execute - but
   it is the only route that plausibly gets to 85%.
2. Accept the public file as-is and write up NO-GO for the
   address-level join, with the 7.9% ceiling as the evidence.

I have not built the address normaliser yet. Given the ceiling above
is a data-availability problem rather than a normalisation problem, I
want your direction before spending more time on it. See questions.
