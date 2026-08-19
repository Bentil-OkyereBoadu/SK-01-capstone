"""Multi-pass deduplication with provenance tracking."""
from pathlib import Path
import pandas as pd
from rapidfuzz import fuzz
import logging

logger = logging.getLogger(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"

DEFAULT_SOURCE_PRIORITY = [
    "globaltech_hris",
    "acquiredco_hris",
    "payroll",
    "benefits",
]


def _source_rank(source: str, priority: list[str]) -> int:
    try:
        return priority.index(source)
    except ValueError:
        return len(priority)


def _merge_source_systems(series: pd.Series) -> str:
    parts: list[str] = []
    for value in series.dropna().astype(str):
        for piece in value.split(","):
            piece = piece.strip()
            if piece and piece not in parts:
                parts.append(piece)
    return ",".join(parts)


def dedup_id(
    df: pd.DataFrame,
    source_col: str = "source_system",
    priority: list[str] | None = None,
) -> pd.DataFrame:
    """Pass 1: exact employee_id match within company namespace.

    When the same namespaced ID appears across sources, keep the highest-priority
    source (HRIS > Payroll > Benefits) and union provenance into source_systems.
    """
    if df.empty:
        return df
    priority = priority or DEFAULT_SOURCE_PRIORITY
    before = len(df)
    work = df.copy()

    if "source_systems" not in work.columns:
        work["source_systems"] = work.get(source_col, pd.Series(["single_source"] * len(work)))
    if source_col not in work.columns:
        work[source_col] = work["source_systems"].astype("string").str.split(",").str[0]

    work["_priority"] = work[source_col].map(lambda s: _source_rank(str(s), priority))
    work = work.sort_values(["employee_id", "_priority"], kind="mergesort")

    provenance = (
        work.groupby("employee_id", sort=False)["source_systems"]
        .agg(_merge_source_systems)
        .rename("_merged_sources")
    )
    kept = work.drop_duplicates(subset=["employee_id"], keep="first").copy()
    kept = kept.drop(columns=["_priority"], errors="ignore")
    kept = kept.merge(provenance, left_on="employee_id", right_index=True, how="left")
    kept["source_systems"] = kept["_merged_sources"].fillna(kept["source_systems"])
    kept = kept.drop(columns=["_merged_sources"], errors="ignore")
    kept["dedup_method"] = "exact_id"

    # Rows that were never duplicated keep single_source unless already set
    dup_ids = df["employee_id"][df["employee_id"].duplicated(keep=False)].unique()
    kept.loc[~kept["employee_id"].isin(dup_ids), "dedup_method"] = kept.loc[
        ~kept["employee_id"].isin(dup_ids), "dedup_method"
    ].where(
        kept.loc[~kept["employee_id"].isin(dup_ids), "dedup_method"].notna(),
        "single_source",
    )
    # Prefer single_source for true singles
    is_single = ~kept["employee_id"].isin(dup_ids)
    kept.loc[is_single, "dedup_method"] = "single_source"

    logger.info("Exact ID dedup: removed %d rows (priority=%s)", before - len(kept), priority)
    return kept.reset_index(drop=True)


def dedup_email(df: pd.DataFrame) -> pd.DataFrame:
    """Pass 2: identical email across companies -> same person; keep first, tag provenance."""
    if df.empty or "email" not in df.columns:
        return df
    before = len(df)
    work = df.copy()
    if "source_systems" not in work.columns:
        work["source_systems"] = work.get("source_system", "unknown")
    if "dedup_method" not in work.columns:
        work["dedup_method"] = "single_source"

    work["_email_key"] = work["email"].astype("string").str.lower().str.strip()
    # Prefer already-priority-sorted frames; otherwise keep first occurrence
    email_sources = (
        work.dropna(subset=["_email_key"])
        .groupby("_email_key", sort=False)["source_systems"]
        .agg(_merge_source_systems)
        .rename("_email_sources")
    )
    dup_emails = work.dropna(subset=["_email_key"])
    dup_emails = dup_emails[dup_emails.duplicated(subset=["_email_key"], keep=False)]["_email_key"].unique()

    if len(dup_emails) > 0:
        logger.warning("Cross-company duplicate emails found: %d", len(dup_emails))

    kept = work.drop_duplicates(subset=["_email_key"], keep="first").copy()
    kept = kept.merge(email_sources, left_on="_email_key", right_index=True, how="left")
    email_matched = kept["_email_key"].isin(dup_emails)
    kept.loc[email_matched, "source_systems"] = kept.loc[email_matched, "_email_sources"]
    kept.loc[email_matched, "dedup_method"] = "email_match"
    kept = kept.drop(columns=["_email_key", "_email_sources"], errors="ignore")

    logger.info("Email dedup: removed %d rows", before - len(kept))
    return kept.reset_index(drop=True)


def find_fuzzy_candidates(
    df: pd.DataFrame,
    threshold: int = 88,
    hire_date_window_days: int = 30,
) -> pd.DataFrame:
    """Pass 3: near-identical names with hire dates within N days.

    Blocks by hire month to avoid O(n^2), then filters pairs to the configured
    hire-date proximity window. Does not auto-merge — returns a review file.
    """
    if df.empty:
        return pd.DataFrame(columns=[
            "record_1_id", "record_2_id", "name_1", "name_2",
            "similarity_score", "hire_date_diff_days", "recommended_action",
        ])

    work = df.copy()
    work["hire_date"] = pd.to_datetime(work["hire_date"], errors="coerce")
    work = work.assign(_block=work["hire_date"].dt.to_period("M"))
    pairs = []
    for _, group in work.groupby("_block"):
        recs = group.to_dict("records")
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                a, b = recs[i], recs[j]
                if pd.isna(a["hire_date"]) or pd.isna(b["hire_date"]):
                    continue
                diff_days = abs((a["hire_date"] - b["hire_date"]).days)
                if diff_days > hire_date_window_days:
                    continue
                score = fuzz.ratio(
                    f"{a['first_name']} {a['last_name']}".lower(),
                    f"{b['first_name']} {b['last_name']}".lower(),
                )
                if score >= threshold:
                    pairs.append({
                        "record_1_id": a["employee_id"],
                        "record_2_id": b["employee_id"],
                        "name_1": f"{a['first_name']} {a['last_name']}",
                        "name_2": f"{b['first_name']} {b['last_name']}",
                        "similarity_score": score,
                        "hire_date_diff_days": int((a["hire_date"] - b["hire_date"]).days),
                        "recommended_action": "HR review",
                    })
    logger.info(
        "Fuzzy candidates: %d pairs (threshold=%d, window=%d days)",
        len(pairs), threshold, hire_date_window_days,
    )
    return pd.DataFrame(pairs)


def detect_ghost_employees(
    payroll: pd.DataFrame,
    hris_ids: pd.Series,
) -> pd.DataFrame:
    """Payroll records with no corresponding HRIS employee_id in the working set.

    Covers true payroll orphans and employees dropped during cleaning
    (e.g., missing department/country) who still appear in payroll.
    """
    ghosts = payroll.loc[~payroll["employee_id"].isin(set(hris_ids))].copy()
    ghosts["ghost_employee"] = True
    ghosts["ghost_flag_reason"] = "Payroll record with no matching HRIS employee"
    logger.info("Ghost employees detected: %d", len(ghosts))
    return ghosts