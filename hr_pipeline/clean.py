"""Cleaning module for the HR integration pipeline."""
from pathlib import Path
import unicodedata
import pandas as pd
import logging

logger = logging.getLogger(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"

EMPLOYMENT_TYPE_MAP = {
    "ft": "Full-Time", "full-time": "Full-Time", "full time": "Full-Time",
    "pt": "Part-Time", "part-time": "Part-Time", "part time": "Part-Time",
    "contractor": "Contractor", "contract": "Contractor",
}

# Unifies GlobalTech-style codes and AcquiredCo/HRIS names into a shared taxonomy.
DEPARTMENT_MAP = {
    # Codes (GlobalTech business-unit style)
    "ENG-01": "Engineering",
    "ENG-02": "Engineering",
    "MKT-01": "Marketing",
    "MKT-03": "Marketing",
    "FIN-01": "Finance",
    "HR-01": "Human Resources",
    "OPS-01": "Operations",
    "IT-01": "Information Technology",
    "SALES-01": "Sales",
    "PROD-01": "Product",
    "DS-01": "Data Science",
    "LEGAL-01": "Legal",
    "CS-01": "Customer Success",
    "QA-01": "Quality Assurance",
    "DEVOPS-01": "DevOps",
    "COMMS-01": "Communications",
    "BD-01": "Business Development",
    "MFG-01": "Manufacturing",
    "SC-01": "Supply Chain",
    "STRAT-01": "Strategy",
    # Name variants
    "engineering": "Engineering",
    "eng": "Engineering",
    "marketing": "Marketing",
    "finance": "Finance",
    "human resources": "Human Resources",
    "hr": "Human Resources",
    "operations": "Operations",
    "ops": "Operations",
    "information technology": "Information Technology",
    "it": "Information Technology",
    "sales": "Sales",
    "product": "Product",
    "data science": "Data Science",
    "legal": "Legal",
    "customer success": "Customer Success",
    "quality assurance": "Quality Assurance",
    "qa": "Quality Assurance",
    "devops": "DevOps",
    "communications": "Communications",
    "business development": "Business Development",
    "manufacturing": "Manufacturing",
    "supply chain": "Supply Chain",
    "strategy": "Strategy",
}

# Particles / connectors that stay lowercase in multi-word last names
_NAME_LOWER_PARTICLES = {"van", "der", "de", "da", "di", "la", "le", "du", "von", "den"}

DEFAULT_EXCHANGE_RATES_TO_USD = {"USD": 1.00, "EUR": 1.08, "GBP": 1.27}
PAY_FREQUENCY_TO_ANNUAL = {"Annual": 1, "Monthly": 12, "Bi-Weekly": 26}


def standardize_text(s: pd.Series) -> pd.Series:
    """Trim whitespace and collapse internal runs of spaces."""
    return s.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)


def _title_name_token(token: str) -> str:
    """Title-case a name token while preserving O'Brien / McDonald / particles."""
    if not token:
        return token
    lower = token.lower()
    if lower in _NAME_LOWER_PARTICLES:
        return lower
    if "'" in token:
        parts = token.split("'")
        return "'".join(p[:1].upper() + p[1:].lower() if p else p for p in parts)
    if "-" in token:
        return "-".join(_title_name_token(p) for p in token.split("-"))
    if lower.startswith("mc") and len(token) > 2:
        return "Mc" + token[2:3].upper() + token[3:].lower()
    return token[:1].upper() + token[1:].lower()


def standardize_names(s: pd.Series) -> pd.Series:
    """Unicode-normalize and title-case names, including hyphenated / multi-word last names.

    Handles accents via NFC normalization, title-cases words, keeps particles like
    'van'/'der' lowercase, and preserves apostrophe patterns (O'Brien).
    """
    def _one(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return pd.NA
        text = unicodedata.normalize("NFC", str(value)).strip()
        text = " ".join(text.split())
        if not text:
            return pd.NA
        return " ".join(_title_name_token(tok) for tok in text.split(" "))

    return s.map(_one).astype("string")


def standardize_employment_type(s: pd.Series) -> pd.Series:
    """Map the many observed spellings onto the canonical three values."""
    cleaned = standardize_text(s).str.lower()
    result = cleaned.map(EMPLOYMENT_TYPE_MAP)
    unmapped = cleaned[result.isna() & cleaned.notna()].unique()
    if len(unmapped) > 0:
        logger.warning("Unmapped employment types: %s", list(unmapped))
    return result


def map_departments(s: pd.Series) -> pd.Series:
    """Map department codes and names onto a unified taxonomy.

    Logs unmapped values for manual review. Values already matching a canonical
    taxonomy name are preserved.
    """
    cleaned = standardize_text(s)
    # Direct code lookup (case-sensitive for codes like ENG-01)
    mapped = cleaned.map(DEPARTMENT_MAP)
    # Name lookup (case-insensitive)
    still = mapped.isna() & cleaned.notna()
    mapped.loc[still] = cleaned.loc[still].str.lower().map(DEPARTMENT_MAP)
    # Preserve already-canonical names
    canonical = set(DEPARTMENT_MAP.values())
    still = mapped.isna() & cleaned.notna()
    preserve = cleaned.loc[still].isin(canonical)
    mapped.loc[still & preserve] = cleaned.loc[still & preserve]

    unmapped = cleaned[mapped.isna() & cleaned.notna()].unique()
    if len(unmapped) > 0:
        logger.warning("Unmapped departments for manual review: %s", list(unmapped))
    return mapped.astype("string")


def parse_salary(s: pd.Series) -> pd.Series:
    """'$85,000.00' / '€64.000' / 85000 -> float. Unparseable -> NaN."""
    txt = s.astype("string").str.replace(r"[^\d.\-]", "", regex=True)
    return pd.to_numeric(txt, errors="coerce")


def to_annual_usd(
    df: pd.DataFrame,
    exchange_rates: dict | None = None,
) -> pd.DataFrame:
    """Adds salary_usd_annual; keeps original columns for auditability.

    Args:
        df: DataFrame with base_salary, currency, pay_frequency columns.
        exchange_rates: Optional currency -> USD rate map (from config).
    """
    rates = exchange_rates or DEFAULT_EXCHANGE_RATES_TO_USD
    df = df.copy()
    amount = parse_salary(df["base_salary"])
    rate = df["currency"].map(rates)
    freq = df["pay_frequency"].map(PAY_FREQUENCY_TO_ANNUAL)
    df["salary_usd_annual"] = (amount * rate * freq).round(2)

    bad = df["salary_usd_annual"].isna() & df["base_salary"].notna()
    logger.info("Salary conversion: %d of %d rows could not be converted", bad.sum(), len(df))
    return df


def parse_dates_multiformat(s: pd.Series) -> pd.Series:
    """Handle YYYY-MM-DD, MM/DD/YYYY, DD-Mon-YYYY, and ISO timestamps."""
    s = s.astype("string").str.strip()
    # Strip trailing Z so UTC ISO stamps stay timezone-naive datetime64[ns]
    s_norm = s.str.replace(r"Z$", "", regex=True)
    result = pd.to_datetime(s_norm, format="%Y-%m-%d", errors="coerce")
    for fmt in ("%m/%d/%Y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        mask = result.isna() & s_norm.notna()
        result.loc[mask] = pd.to_datetime(s_norm[mask], format=fmt, errors="coerce")
    # Final fallback for any remaining parseable strings
    still = result.isna() & s_norm.notna()
    if still.any():
        fallback = pd.to_datetime(s_norm[still], errors="coerce", utc=True)
        result.loc[still] = fallback.dt.tz_localize(None)
    still_bad = result.isna() & s.notna()
    logger.info("Date parsing: %d unparseable values", still_bad.sum())
    return result

def flag_implausible_dates(s: pd.Series) -> pd.Series:
    """True where a date is before 1970 or in the future."""
    return (s < pd.Timestamp("1970-01-01")) | (s > pd.Timestamp.now())


def namespace_ids(df: pd.DataFrame, prefix: str, id_col: str = "employee_id") -> pd.DataFrame:
    """1042 -> 'GT-001042' so IDs from different companies can never collide."""
    df = df.copy()
    cleaned = df[id_col].astype("string").str.strip().str.replace(r"\s+", "", regex=True)
    digits = cleaned.str.extract(r"(\d+)")[0]
    df[id_col] = prefix + "-" + digits.str.zfill(6)
    return df


def flag_null_department(df: pd.DataFrame, output_path: Path) -> pd.DataFrame:
    """Flag employees with null department, save to CSV, and remove from df."""
    df = df.copy()
    flagged = df[df["department"].isna()].copy()
    flagged["flag_reason"] = "Missing department"

    if len(flagged) > 0:
        flagged.to_csv(output_path, index=False)
        logger.info("Flagged %d employees with missing department: %s", len(flagged), output_path)
    else:
        logger.info("No employees with missing department found")

    return df[df["department"].notna()]


def flag_null_country(df: pd.DataFrame, output_path: Path) -> pd.DataFrame:
    """Flag employees with null country, save to CSV, and remove from df."""
    df = df.copy()
    flagged = df[df["country"].isna()].copy()
    flagged["flag_reason"] = "Missing country"

    if len(flagged) > 0:
        flagged.to_csv(output_path, index=False)
        logger.info("Flagged %d employees with missing country: %s", len(flagged), output_path)
    else:
        logger.info("No employees with missing country found")

    return df[df["country"].notna()]
