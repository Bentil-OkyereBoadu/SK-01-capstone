"""Ingestion module: reads all four HR sources into standardized DataFrames.

Schema alignment decisions (documented for Deliverable 1):
- GlobalTech HRIS: job_title -> role; remaining columns already match the
  standard employee schema (employee_id, first_name, last_name, email,
  department, hire_date, country, employment_type, manager_id).
- AcquiredCo JSON: nested API fields flattened then remapped —
  employee_identifier -> employee_id, name_first/last/full -> first/last/full_name,
  contact_email -> email, assignment_department/role/location/hire_timestamp ->
  department/role/country/hire_date, employment_type/status kept, manager_employee_id
  -> manager_id.
- Payroll: employee_id, base_salary, currency, pay_frequency, bonus_target_pct,
  effective_date retained; source column tagged as payroll.
- Benefits: employee_id, plan_type, coverage_level, enrollment_date,
  premium_employee, premium_employer retained; source tagged as benefits.
"""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
import pandas as pd
import logging

from hr_pipeline.retry import retry

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "raw"
REJECTED_DIR = Path(__file__).parent.parent / "data" / "rejected"

# Mapping decisions: source column -> standard schema column
SCHEMA_MAPS = {
    "globaltech_hris": {
        "job_title": "role",
    },
    "acquiredco_hris": {
        "employee_identifier": "employee_id",
        "manager_employee_id": "manager_id",
        "name_first": "first_name",
        "name_last": "last_name",
        "name_full": "full_name",
        "contact_email": "email",
        "assignment_department": "department",
        "assignment_role": "role",
        "assignment_hire_timestamp": "hire_date",
        "assignment_location": "country",
        "employment_type": "employment_type",
        "employment_status": "employment_status",
    },
    "payroll": {},  # already aligned to payroll subset of standard schema
    "benefits": {},  # already aligned to benefits subset of standard schema
}


def _append_dead_letter(rows: list[dict], reason: str, source: str) -> None:
    """Append malformed records to the dead-letter CSV without crashing."""
    if not rows:
        return
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    path = REJECTED_DIR / "dead_letter.csv"
    frame = pd.DataFrame(rows)
    frame["dead_letter_reason"] = reason
    frame["source_system"] = source
    write_header = not path.exists()
    frame.to_csv(path, mode="a", header=write_header, index=False)
    logger.warning("Dead-lettered %d records from %s (%s)", len(rows), source, reason)


def align_schema(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Map a source DataFrame onto the standard employee schema.

    Every rename decision is recorded in SCHEMA_MAPS above. Columns not listed
    are left unchanged. Adds a source_system tag for provenance.

    Args:
        df: Raw ingested DataFrame for one source.
        source: Logical source name (globaltech_hris, acquiredco_hris, payroll, benefits).

    Returns:
        Schema-aligned DataFrame with a source_system column.
    """
    if source not in SCHEMA_MAPS:
        raise ValueError(f"Unknown source for schema alignment: {source}")
    out = df.rename(columns=SCHEMA_MAPS[source]).copy()
    out["source_system"] = source
    logger.info(
        "Schema aligned for %s: %d columns -> %d rows tagged as source_system=%s",
        source, len(out.columns), len(out), source,
    )
    return out


@retry(times=3)
def ingest_hris_csv(path: Path) -> pd.DataFrame:
    """Load GlobalTech HRIS export (CSV UTF-8).

    Args:
        path: Path to globaltech_hris.csv.

    Returns:
        DataFrame of GlobalTech employees with source_system tagging.
    """
    try:
        df = pd.read_csv(
            path,
            dtype={"employee_id": str, "manager_id": str},
            parse_dates=["hire_date"],
        )
    except FileNotFoundError:
        logger.error("Missing HRIS file: %s", path)
        _append_dead_letter([{"path": str(path)}], "missing_file", "globaltech_hris")
        return pd.DataFrame()
    except Exception as exc:
        logger.error("Failed to read HRIS CSV %s: %s", path, exc)
        _append_dead_letter([{"path": str(path), "error": str(exc)}], "malformed_file", "globaltech_hris")
        return pd.DataFrame()

    logger.info("Ingested globaltech_hris: %d records from %s", len(df), path)
    return align_schema(df, "globaltech_hris")


@retry(times=3)
def ingest_acquiredco_json(path: Path, page_size: int = 500) -> pd.DataFrame:
    """Load AcquiredCo BambooHR-style API dump with simulated pagination.

    The file is a single JSON payload, but we page through `employees` in
    chunks of `page_size` to mirror a real paginated API client.

    Args:
        path: Path to acquiredco_api.json.
        page_size: Simulated page size (default 500).

    Returns:
        Concatenated DataFrame of all pages, schema-aligned.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        logger.error("Missing AcquiredCo file: %s", path)
        _append_dead_letter([{"path": str(path)}], "missing_file", "acquiredco_hris")
        return pd.DataFrame()
    except json.JSONDecodeError as exc:
        logger.error("Malformed AcquiredCo JSON %s: %s", path, exc)
        _append_dead_letter([{"path": str(path), "error": str(exc)}], "malformed_file", "acquiredco_hris")
        return pd.DataFrame()

    employees = payload.get("employees", [])
    expected = payload.get("total_records", len(employees))
    pages: list[pd.DataFrame] = []
    dead: list[dict] = []

    for page_num, start in enumerate(range(0, len(employees), page_size), start=1):
        chunk = employees[start:start + page_size]
        logger.info(
            "AcquiredCo API page %d: fetched %d records (offset=%d)",
            page_num, len(chunk), start,
        )
        try:
            page_df = pd.json_normalize(chunk)
            page_df.columns = [c.replace(".", "_") for c in page_df.columns]
            pages.append(page_df)
        except Exception as exc:
            logger.warning("Failed to normalize AcquiredCo page %d: %s", page_num, exc)
            dead.extend(chunk if isinstance(chunk, list) else [{"page": page_num, "error": str(exc)}])

    _append_dead_letter(dead, "malformed_page", "acquiredco_hris")

    if not pages:
        logger.warning("AcquiredCo ingestion produced zero pages")
        return pd.DataFrame()

    df = pd.concat(pages, ignore_index=True)
    if len(df) != expected:
        logger.warning(
            "Record count mismatch for AcquiredCo: got %d, API said %d — continuing with available records",
            len(df), expected,
        )
    logger.info("Ingested acquiredco_hris: %d records across %d pages", len(df), len(pages))
    return align_schema(df, "acquiredco_hris")


@retry(times=3)
def ingest_payroll_excel(path: Path) -> pd.DataFrame:
    """Load combined payroll export (Excel .xlsx).

    Args:
        path: Path to payroll_data.xlsx.

    Returns:
        Payroll DataFrame with coerced salary/date columns and source tagging.
    """
    try:
        sheets = pd.ExcelFile(path).sheet_names
        df = pd.read_excel(
            path,
            sheet_name=sheets[0],
            dtype={"employee_id": "string"},
        )
    except FileNotFoundError:
        logger.error("Missing payroll file: %s", path)
        _append_dead_letter([{"path": str(path)}], "missing_file", "payroll")
        return pd.DataFrame()
    except Exception as exc:
        logger.error("Failed to read payroll Excel %s: %s", path, exc)
        _append_dead_letter([{"path": str(path), "error": str(exc)}], "malformed_file", "payroll")
        return pd.DataFrame()

    df["employee_id"] = df["employee_id"].astype("string")
    df["base_salary"] = pd.to_numeric(
        df["base_salary"].astype("string").str.replace(r"[^0-9.-]", "", regex=True),
        errors="coerce",
    )
    df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")

    bad_mask = df["employee_id"].isna()
    if bad_mask.any():
        _append_dead_letter(df.loc[bad_mask].to_dict("records"), "missing_employee_id", "payroll")
        df = df.loc[~bad_mask].copy()

    logger.info("Ingested payroll: %d records from %s", len(df), path)
    return align_schema(df, "payroll")


@retry(times=3)
def ingest_benefits_xml(path: Path) -> pd.DataFrame:
    """Load MedShield benefits enrollment (XML) via ElementTree.

    Args:
        path: Path to benefits_enrollment.xml.

    Returns:
        Benefits enrollment DataFrame with numeric premiums coerced.
    """
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        logger.error("Missing benefits file: %s", path)
        _append_dead_letter([{"path": str(path)}], "missing_file", "benefits")
        return pd.DataFrame()
    except ET.ParseError as exc:
        logger.error("Malformed benefits XML %s: %s", path, exc)
        _append_dead_letter([{"path": str(path), "error": str(exc)}], "malformed_file", "benefits")
        return pd.DataFrame()

    records = []
    dead = []
    for idx, node in enumerate(root.findall("enrollment")):
        try:
            records.append({child.tag: child.text for child in node})
        except Exception as exc:
            dead.append({"index": idx, "error": str(exc)})

    _append_dead_letter(dead, "malformed_enrollment", "benefits")
    df = pd.DataFrame(records)
    for col in ("premium_employee", "premium_employer"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    logger.info("Ingested benefits: %d records from %s", len(df), path)
    return align_schema(df, "benefits")
