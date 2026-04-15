#!/usr/bin/env python3
"""Download NASA spending data from USAspending.gov and build a SQLite database.

This script is designed to run inside a sandbox environment with only Python
stdlib available. It fetches data via the USAspending bulk download API,
parses the resulting CSVs, and creates a local SQLite database.

Usage:
    python setup_db.py [--force] [--start-fy 2021] [--end-fy 2025]

The script is idempotent: it skips the download/build if the database already
exists unless --force is passed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

DB_DIR = Path("data")
DB_PATH = DB_DIR / "usaspending.db"
GLOSSARY_PATH = Path("schema") / "glossary.md"

USASPENDING_API = "https://api.usaspending.gov"
BULK_DOWNLOAD_ENDPOINT = f"{USASPENDING_API}/api/v2/bulk_download/awards/"
DOWNLOAD_STATUS_ENDPOINT = f"{USASPENDING_API}/api/v2/download/status"
GLOSSARY_ENDPOINT = f"{USASPENDING_API}/api/v2/references/glossary/"

NASA_AGENCY = {
    "type": "awarding",
    "tier": "toptier",
    "name": "National Aeronautics and Space Administration",
}

# Award type codes per the USAspending API contract.
CONTRACT_CODES = ["A", "B", "C", "D"]
GRANT_CODES = ["02", "03", "04", "05"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
ALL_AWARD_CODES = CONTRACT_CODES + GRANT_CODES + IDV_CODES

AWARD_TYPE_MAP: dict[str, str] = {}
for _code in CONTRACT_CODES:
    AWARD_TYPE_MAP[_code] = "contract"
for _code in GRANT_CODES:
    AWARD_TYPE_MAP[_code] = "grant"
for _code in IDV_CODES:
    AWARD_TYPE_MAP[_code] = "idv"

# Common headers — the USAspending WAF rejects requests without a User-Agent.
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "USAspending-setup/1.0 (universal_computer example)",
    "Accept": "application/json",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spending (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    award_id TEXT,
    award_piid_fain TEXT,
    parent_award_piid TEXT,
    award_type TEXT,
    description TEXT,
    action_date TEXT,
    fiscal_year INTEGER,
    federal_action_obligation REAL,
    total_obligation REAL,
    base_and_all_options_value REAL,
    recipient_name TEXT,
    recipient_parent_name TEXT,
    recipient_state TEXT,
    recipient_city TEXT,
    recipient_country TEXT,
    awarding_office TEXT,
    funding_office TEXT,
    naics_code TEXT,
    naics_description TEXT,
    psc_code TEXT,
    psc_description TEXT,
    place_of_performance_state TEXT,
    place_of_performance_city TEXT,
    period_of_perf_start TEXT,
    period_of_perf_end TEXT,
    extent_competed TEXT,
    type_of_set_aside TEXT,
    number_of_offers INTEGER,
    contract_pricing_type TEXT,
    business_types TEXT
);

CREATE INDEX IF NOT EXISTS idx_spending_award_id ON spending(award_id);
CREATE INDEX IF NOT EXISTS idx_spending_fiscal_year ON spending(fiscal_year);
CREATE INDEX IF NOT EXISTS idx_spending_award_type ON spending(award_type);
CREATE INDEX IF NOT EXISTS idx_spending_recipient ON spending(recipient_name);
CREATE INDEX IF NOT EXISTS idx_spending_recipient_parent ON spending(recipient_parent_name);
CREATE INDEX IF NOT EXISTS idx_spending_state ON spending(recipient_state);
CREATE INDEX IF NOT EXISTS idx_spending_action_date ON spending(action_date);
CREATE INDEX IF NOT EXISTS idx_spending_naics ON spending(naics_code);
CREATE INDEX IF NOT EXISTS idx_spending_obligation ON spending(federal_action_obligation);
CREATE INDEX IF NOT EXISTS idx_spending_extent_competed ON spending(extent_competed);
CREATE INDEX IF NOT EXISTS idx_spending_perf_start ON spending(period_of_perf_start);
CREATE INDEX IF NOT EXISTS idx_spending_awarding_office ON spending(awarding_office);
"""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _urlopen_with_retry(
    req: urllib.request.Request, *, timeout: int = 60, retries: int = 3
) -> bytes:
    """urlopen with retries for the flaky USAspending endpoints."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return bytes(resp.read())
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_exc = e
            if attempt < retries:
                wait = 2**attempt
                print(f"    Retry {attempt}/{retries} after error: {e} (waiting {wait}s)")
                time.sleep(wait)
    raise RuntimeError(f"Request failed after {retries} attempts: {last_exc}") from last_exc


def api_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to a USAspending API endpoint and return the parsed response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_HEADERS, method="POST")
    body = _urlopen_with_retry(req)
    return json.loads(body.decode("utf-8"))  # type: ignore[no-any-return]


def api_get(url: str) -> dict[str, Any]:
    """GET a USAspending API endpoint and return the parsed response."""
    req = urllib.request.Request(url, headers=_HEADERS)
    body = _urlopen_with_retry(req)
    return json.loads(body.decode("utf-8"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Bulk download
# ---------------------------------------------------------------------------


def submit_bulk_download(
    award_types: list[str],
    start_date: str,
    end_date: str,
) -> tuple[str | None, str | None]:
    """Submit a bulk download request and return (status_url, file_url).

    The USAspending bulk download API requires:
    - filters.agencies: list of agency objects (name/tier/type)
    - filters.prime_award_types: list of award type codes
    - filters.date_type: "action_date" or "last_modified_date"
    - filters.date_range: {start_date, end_date} (max 1 year span)

    This only submits the request — call poll_download_status() to wait for completion.
    """
    payload = {
        "filters": {
            "agencies": [NASA_AGENCY],
            "prime_award_types": award_types,
            "date_type": "action_date",
            "date_range": {
                "start_date": start_date,
                "end_date": end_date,
            },
        },
        "file_format": "csv",
    }

    resp = api_post(BULK_DOWNLOAD_ENDPOINT, payload)
    file_url = resp.get("file_url")
    status_url = resp.get("status_url")

    if not status_url and not file_url:
        raise RuntimeError(f"Unexpected API response: {resp}")

    return status_url, file_url


def poll_download_status(status_url: str | None, file_url: str | None) -> str:
    """Poll the download status endpoint until the file is ready."""
    if not status_url:
        if file_url:
            return file_url
        raise RuntimeError("No status_url or file_url to poll")

    for attempt in range(120):
        try:
            status = api_get(status_url)
        except Exception:
            time.sleep(5)
            continue

        state = status.get("status", "unknown")
        if state == "finished":
            return status.get("file_url") or file_url or ""
        elif state == "failed":
            raise RuntimeError(f"Download generation failed: {status.get('message', 'unknown')}")

        if attempt % 6 == 0:
            print(f"    Generating... (status: {state})")
        time.sleep(5)

    raise RuntimeError("Timed out waiting for download (10 minutes)")


def download_and_extract(file_url: str, extract_dir: Path) -> list[Path]:
    """Download a zip file and extract CSVs to extract_dir."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    zip_path = extract_dir / "download.zip"

    print("  Downloading...")
    req = urllib.request.Request(file_url, headers={"User-Agent": _HEADERS["User-Agent"]})
    data = _urlopen_with_retry(req, timeout=300, retries=3)
    zip_path.write_bytes(data)
    file_size_mb = len(data) / (1024 * 1024)
    print(f"  Downloaded {file_size_mb:.1f} MB")

    print("  Extracting CSV files...")
    csv_files = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith(".csv"):
                zf.extract(name, extract_dir)
                csv_files.append(extract_dir / name)
                print(f"    {name}")

    zip_path.unlink()
    return csv_files


# ---------------------------------------------------------------------------
# CSV ingestion
# ---------------------------------------------------------------------------


def safe_float(val: str) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val.replace(",", ""))
    except ValueError:
        return None


def safe_int(val: str) -> int | None:
    if not val or val.strip() == "":
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


def classify_award_type(type_code: str, award_id: str) -> str:
    mapped = AWARD_TYPE_MAP.get(type_code)
    if mapped:
        return mapped
    # Fallback: detect IDVs from the award_id prefix when the type code
    # doesn't match our expected IDV codes.
    if award_id.startswith("CONT_IDV_"):
        return "idv"
    return "other"


def _detect_csv_type(headers: set[str]) -> str:
    """Detect whether a CSV is contracts or assistance based on its headers.

    Per the USAspending data dictionary, PrimeAwardUniqueKey is stored as
    'contract_award_unique_key' in contracts and 'assistance_award_unique_key'
    in assistance.
    """
    if "contract_award_unique_key" in headers:
        return "contracts"
    if "assistance_award_unique_key" in headers:
        return "assistance"
    raise ValueError(
        "Cannot detect CSV type: neither 'contract_award_unique_key' nor "
        "'assistance_award_unique_key' found in headers"
    )


# Column mappings per CSV type, derived from the USAspending data dictionary
# (https://api.usaspending.gov/api/v2/references/data_dictionary/).
#
# "shared" columns have the same name in both contracts and assistance CSVs.
# Type-specific columns are listed under "contracts" and "assistance".

# Column mappings verified against actual CSV headers downloaded from USAspending
# on 2026-03-26, and cross-referenced with the data dictionary API at
# https://api.usaspending.gov/api/v2/references/data_dictionary/.
#
# "shared" columns have the same name in both contracts and assistance CSVs.
# Type-specific columns differ between the two and are listed separately.

_SHARED_COLUMNS = {
    # db_column                    -> csv_column
    "action_date": "action_date",
    "fiscal_year": "action_date_fiscal_year",
    "federal_action_obligation": "federal_action_obligation",
    "recipient_name": "recipient_name",
    "recipient_state": "recipient_state_code",
    "recipient_city": "recipient_city_name",
    "recipient_country": "recipient_country_name",
    "awarding_office": "awarding_office_name",
    "funding_office": "funding_office_name",
    "description": "transaction_description",
    "place_of_performance_city": "primary_place_of_performance_city_name",
    "period_of_perf_start": "period_of_performance_start_date",
    "period_of_perf_end": "period_of_performance_current_end_date",
}

_TYPE_COLUMNS: dict[str, dict[str, str]] = {
    "contracts": {
        "award_id": "contract_award_unique_key",
        "award_piid_fain": "award_id_piid",
        "parent_award_piid": "parent_award_id_piid",
        "award_type_code": "award_type_code",
        "total_obligation": "total_dollars_obligated",
        "base_and_all_options_value": "base_and_all_options_value",
        "recipient_parent_name": "recipient_parent_name",
        "place_of_performance_state": "primary_place_of_performance_state_code",
        "naics_code": "naics_code",
        "naics_description": "naics_description",
        "psc_code": "product_or_service_code",
        "psc_description": "product_or_service_code_description",
        "extent_competed": "extent_competed",
        "type_of_set_aside": "type_of_set_aside",
        "number_of_offers": "number_of_offers_received",
        "contract_pricing_type": "type_of_contract_pricing",
        "business_types": "",  # not present in contracts CSVs
    },
    "assistance": {
        "award_id": "assistance_award_unique_key",
        "award_piid_fain": "award_id_fain",
        "parent_award_piid": "",  # not applicable to assistance
        "award_type_code": "assistance_type_code",
        "total_obligation": "total_obligated_amount",
        "base_and_all_options_value": "",  # contracts only
        "recipient_parent_name": "",  # contracts only
        "place_of_performance_state": "primary_place_of_performance_state_name",
        "naics_code": "",  # not present in assistance CSVs
        "naics_description": "",
        "psc_code": "cfda_number",
        "psc_description": "cfda_title",
        "extent_competed": "",  # contracts only
        "type_of_set_aside": "",  # contracts only
        "number_of_offers": "",  # contracts only
        "contract_pricing_type": "",  # contracts only
        "business_types": "business_types_description",
    },
}


def ingest_csv(db: sqlite3.Connection, csv_path: Path) -> int:
    """Ingest a USAspending prime transactions CSV into the spending table."""
    count = 0

    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return 0

        headers = set(reader.fieldnames)
        csv_type = _detect_csv_type(headers)
        type_cols = _TYPE_COLUMNS[csv_type]

        # Verify expected columns exist
        all_expected = dict(_SHARED_COLUMNS)
        all_expected.update(type_cols)
        missing = [
            db_col for db_col, csv_col in all_expected.items() if csv_col and csv_col not in headers
        ]
        if missing:
            print(f"    Warning: missing expected columns: {missing}")

        award_id_col = type_cols["award_id"]
        award_type_col = type_cols["award_type_code"]

        for row in reader:
            award_id = row.get(award_id_col, "")
            if not award_id:
                continue

            type_code = row.get(award_type_col, "")
            award_type = classify_award_type(type_code, award_id)

            def col(db_name: str, _row: dict[str, str] = row) -> str:
                """Look up a value: type-specific columns first, then shared."""
                csv_col = type_cols.get(db_name) or _SHARED_COLUMNS.get(db_name, "")
                return _row.get(csv_col, "") if csv_col else ""

            db.execute(
                """INSERT INTO spending
                   (award_id, award_piid_fain, parent_award_piid,
                    award_type, description, action_date, fiscal_year,
                    federal_action_obligation, total_obligation, base_and_all_options_value,
                    recipient_name, recipient_parent_name,
                    recipient_state, recipient_city, recipient_country,
                    awarding_office, funding_office,
                    naics_code, naics_description, psc_code, psc_description,
                    place_of_performance_state, place_of_performance_city,
                    period_of_perf_start, period_of_perf_end,
                    extent_competed, type_of_set_aside, number_of_offers,
                    contract_pricing_type, business_types)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    award_id,
                    col("award_piid_fain"),
                    col("parent_award_piid"),
                    award_type,
                    col("description"),
                    col("action_date"),
                    safe_int(col("fiscal_year")),
                    safe_float(col("federal_action_obligation")),
                    safe_float(col("total_obligation")),
                    safe_float(col("base_and_all_options_value")),
                    col("recipient_name"),
                    col("recipient_parent_name"),
                    col("recipient_state"),
                    col("recipient_city"),
                    col("recipient_country"),
                    col("awarding_office"),
                    col("funding_office"),
                    col("naics_code"),
                    col("naics_description"),
                    col("psc_code"),
                    col("psc_description"),
                    col("place_of_performance_state"),
                    col("place_of_performance_city"),
                    col("period_of_perf_start"),
                    col("period_of_perf_end"),
                    col("extent_competed"),
                    col("type_of_set_aside"),
                    safe_int(col("number_of_offers")),
                    col("contract_pricing_type"),
                    col("business_types"),
                ),
            )
            count += 1

    return count


def build_database(csv_files: list[Path]) -> None:
    """Build the SQLite database from extracted CSV files."""
    DB_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Creating database at {DB_PATH}...")
    db = sqlite3.connect(str(DB_PATH))
    db.executescript(SCHEMA_SQL)

    total = 0
    for csv_path in csv_files:
        print(f"  Ingesting {csv_path.name}...")
        count = ingest_csv(db, csv_path)
        total += count
        print(f"    {count:,} rows")

    db.commit()

    cursor = db.execute("SELECT COUNT(*) FROM spending")
    rows_stored = cursor.fetchone()[0]
    cursor = db.execute("SELECT COUNT(DISTINCT award_id) FROM spending")
    unique_awards = cursor.fetchone()[0]
    db.close()

    db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"\nDatabase built: {DB_PATH}")
    print(f"  Rows:           {rows_stored:,}")
    print(f"  Unique awards:  {unique_awards:,}")
    print(f"  Size:           {db_size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------


def fetch_glossary() -> None:
    """Fetch the official USAspending glossary and write it to schema/glossary.md."""
    if GLOSSARY_PATH.exists():
        print(f"Glossary already exists at {GLOSSARY_PATH}, skipping.")
        return

    GLOSSARY_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Fetching USAspending glossary...")
    try:
        resp = api_get(f"{GLOSSARY_ENDPOINT}?limit=500")
    except Exception as e:
        print(f"  Warning: failed to fetch glossary: {e}")
        return

    results = resp.get("results", [])
    if not results:
        print("  Warning: glossary API returned no results.")
        return

    results.sort(key=lambda t: t.get("term", "").lower())

    lines = [
        "# USAspending Glossary",
        "",
        "Official definitions from [USAspending.gov](https://www.usaspending.gov).",
        f"Retrieved automatically by setup_db.py ({len(results)} terms).",
        "",
    ]

    for entry in results:
        term = entry.get("term", "").strip()
        plain = (entry.get("plain") or "").strip()
        official = (entry.get("official") or "").strip()

        if not term:
            continue

        lines.append(f"## {term}")
        lines.append("")
        if plain:
            lines.append(plain)
            lines.append("")
        if official and official != plain:
            lines.append(f"**Official definition:** {official}")
            lines.append("")

    GLOSSARY_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Wrote {len(results)} glossary terms to {GLOSSARY_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fiscal_year_dates(fy: int) -> tuple[str, str]:
    """Return (start_date, end_date) for a federal fiscal year.

    Federal FY runs Oct 1 of the prior calendar year through Sep 30.
    Example: FY2024 = 2023-10-01 to 2024-09-30.
    """
    return f"{fy - 1}-10-01", f"{fy}-09-30"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build NASA USAspending SQLite database")
    parser.add_argument("--force", action="store_true", help="Rebuild even if database exists")
    parser.add_argument(
        "--start-fy", type=int, default=2021, help="First fiscal year to download (default: 2021)"
    )
    parser.add_argument(
        "--end-fy", type=int, default=2025, help="Last fiscal year to download (default: 2025)"
    )
    args = parser.parse_args()

    if args.start_fy > args.end_fy:
        parser.error(f"--start-fy ({args.start_fy}) must be <= --end-fy ({args.end_fy})")

    requested_fys = set(range(args.start_fy, args.end_fy + 1))

    if DB_PATH.exists() and not args.force:
        # Verify the existing DB covers all requested fiscal years.
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute("SELECT DISTINCT fiscal_year FROM spending").fetchall()
            conn.close()
            present_fys = {int(r[0]) for r in rows if r[0] is not None}
            missing_fys = requested_fys - present_fys
            if not missing_fys:
                db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
                print(
                    f"Database already exists at {DB_PATH} ({db_size_mb:.1f} MB) "
                    f"with all requested FYs. Use --force to rebuild."
                )
                return
            print(
                f"Database exists but is missing FY data for: "
                f"{', '.join(str(fy) for fy in sorted(missing_fys))}. Rebuilding..."
            )
        except Exception:
            print("Database exists but could not be verified. Rebuilding...")
        DB_PATH.unlink()
    elif DB_PATH.exists():
        DB_PATH.unlink()

    tmp_dir = Path("data/tmp_download")

    print("=== NASA USAspending Database Builder ===")
    print(f"Fiscal years: {args.start_fy} - {args.end_fy}\n")

    # The bulk download API limits date_range to 1 year, so we request
    # one fiscal year at a time. We submit all requests upfront so the
    # server-side assembly (the slow part) runs concurrently, then poll
    # and download the results.
    all_csv_files: list[Path] = []
    failed_fys: list[int] = []
    fiscal_years = list(range(args.start_fy, args.end_fy + 1))

    # Phase 1: Submit all bulk download requests concurrently.
    print("Submitting download requests...")
    pending: dict[int, tuple[str | None, str | None]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fiscal_years)) as pool:

        def _submit(fy: int) -> tuple[int, str | None, str | None]:
            start_date, end_date = fiscal_year_dates(fy)
            status_url, file_url = submit_bulk_download(
                ALL_AWARD_CODES,
                start_date,
                end_date,
            )
            return fy, status_url, file_url

        futures = {pool.submit(_submit, fy): fy for fy in fiscal_years}
        for future in concurrent.futures.as_completed(futures):
            fy = futures[future]
            try:
                _, status_url, file_url = future.result()
                pending[fy] = (status_url, file_url)
                print(f"  FY{fy}: submitted")
            except Exception as e:
                print(f"  FY{fy}: submit failed: {e}")
                failed_fys.append(fy)

    # Phase 2: Poll all pending requests until ready, then download.
    for fy in sorted(pending):
        print(f"\n--- FY{fy} ---")
        status_url, file_url = pending[fy]
        try:
            file_url = poll_download_status(status_url, file_url)
            print(f"  Ready: {file_url}")
            fy_dir = tmp_dir / f"fy{fy}"
            csv_files = download_and_extract(file_url, fy_dir)
            all_csv_files.extend(csv_files)
        except Exception as e:
            print(f"  Error: failed FY{fy}: {e}")
            failed_fys.append(fy)

    if not all_csv_files:
        print("\nError: no data downloaded. Check internet connectivity.")
        sys.exit(1)

    if failed_fys:
        print(
            f"\nError: failed to download data for: "
            f"{', '.join(f'FY{fy}' for fy in failed_fys)}. "
            f"Cannot build a complete database."
        )
        sys.exit(1)

    print("\n--- Fetching glossary ---")
    fetch_glossary()

    print("\n--- Building database ---")
    build_database(all_csv_files)

    # Verify the built DB covers all requested fiscal years.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("SELECT DISTINCT fiscal_year FROM spending").fetchall()
    conn.close()
    present_fys = {int(r[0]) for r in rows if r[0] is not None}
    missing_fys = requested_fys - present_fys
    if missing_fys:
        print(
            f"\nError: database built but missing data for: "
            f"{', '.join(f'FY{fy}' for fy in sorted(missing_fys))}. "
            f"Downloaded files may have been empty."
        )
        DB_PATH.unlink()
        sys.exit(1)

    # Clean up temp files
    for f in tmp_dir.rglob("*"):
        if f.is_file():
            f.unlink()
    for d in sorted(tmp_dir.rglob("*"), reverse=True):
        if d.is_dir():
            d.rmdir()
    if tmp_dir.exists():
        tmp_dir.rmdir()

    print("\nDone!")


if __name__ == "__main__":
    main()
