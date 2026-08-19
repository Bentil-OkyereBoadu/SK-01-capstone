"""HR Integration Pipeline — entry point.

Usage:  python run_pipeline.py
"""
from pathlib import Path
import sys
import pandas as pd
import logging

from hr_pipeline import ingest, clean, dedup, validate, load, graph
from hr_pipeline.logging_setup import setup_logging
from hr_pipeline.config import load_config


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
setup_logging(BASE_DIR / "logs")
cfg = load_config(BASE_DIR / "config" / "pipeline.yaml")
RAW = BASE_DIR / "data" / "raw"
PROCESSED = BASE_DIR / "data" / "processed"
REJECTED = BASE_DIR / "data" / "rejected"
OUTPUT = BASE_DIR / "output"


def run(config: dict) -> int:
    expected_files = [
        RAW / "globaltech_hris.csv",
        RAW / "acquiredco_api.json",
        RAW / "payroll_data.xlsx",
        RAW / "benefits_enrollment.xml",
    ]
    missing = [f.name for f in expected_files if not f.exists()]
    if missing:
        logger.critical("FATAL: missing input files: %s", missing)
        return 1

    for d in (PROCESSED, REJECTED, OUTPUT):
        d.mkdir(parents=True, exist_ok=True)

    # Clear prior dead-letter file for this run
    dead_letter = REJECTED / "dead_letter.csv"
    if dead_letter.exists():
        dead_letter.unlink()

    page_size = config.get("ingest", {}).get("page_size", 500)
    rates = config["exchange_rates_to_usd"]
    dedup_cfg = config["dedup"]
    gate_cfg = config["quality_gate"]
    salary_bounds = config.get("salary_bounds", {"min": 15_000, "max": 2_000_000})
    source_priority = dedup_cfg.get(
        "source_priority",
        ["globaltech_hris", "acquiredco_hris", "payroll", "benefits"],
    )

    # ---- EXTRACT -------------------------------------------------
    logger.info("[1/6] Extract")
    hris = ingest.ingest_hris_csv(RAW / "globaltech_hris.csv")
    acq = ingest.ingest_acquiredco_json(RAW / "acquiredco_api.json", page_size=page_size)
    payroll = ingest.ingest_payroll_excel(RAW / "payroll_data.xlsx")
    benefits = ingest.ingest_benefits_xml(RAW / "benefits_enrollment.xml")

    logger.info(
        "      hris=%s  acquiredco=%s  payroll=%s  benefits=%s",
        f"{len(hris):,}", f"{len(acq):,}", f"{len(payroll):,}", f"{len(benefits):,}",
    )

    # ---- TRANSFORM: standardize each source ----------------------
    logger.info("[2/6] Clean & standardize")

    hris = clean.namespace_ids(hris, "GT")
    hris = clean.namespace_ids(hris, "GT", "manager_id")
    hris["first_name"] = clean.standardize_names(hris["first_name"])
    hris["last_name"] = clean.standardize_names(hris["last_name"])
    hris["department"] = clean.map_departments(hris["department"])
    hris["hire_date"] = clean.parse_dates_multiformat(hris["hire_date"])
    hris["employment_type"] = clean.standardize_employment_type(hris["employment_type"])
    hris["company_origin"] = "GlobalTech"
    hris["source_systems"] = hris["source_system"]
    hris["dedup_method"] = "single_source"

    acq = clean.namespace_ids(acq, "AC")
    acq = clean.namespace_ids(acq, "AC", "manager_id")
    acq["first_name"] = clean.standardize_names(acq["first_name"])
    acq["last_name"] = clean.standardize_names(acq["last_name"])
    acq["department"] = clean.map_departments(acq["department"])
    acq["hire_date"] = clean.parse_dates_multiformat(acq["hire_date"])
    acq["employment_type"] = clean.standardize_employment_type(acq["employment_type"])
    acq["company_origin"] = "AcquiredCo"
    acq["source_systems"] = acq["source_system"]
    acq["dedup_method"] = "single_source"

    payroll = clean.namespace_ids(payroll, "GT")
    payroll["source_systems"] = payroll["source_system"]

    benefits = clean.namespace_ids(benefits, "GT")
    benefits["enrollment_date"] = clean.parse_dates_multiformat(benefits["enrollment_date"])
    benefits["source_systems"] = benefits["source_system"]

    # Flag implausible hire dates (log + keep for review, do not crash)
    for label, frame in (("hris", hris), ("acquiredco", acq)):
        bad = clean.flag_implausible_dates(frame["hire_date"])
        if bad.any():
            logger.warning("%s: %d implausible hire_date values flagged", label, int(bad.sum()))
            frame.loc[bad, "implausible_hire_date"] = True
        else:
            frame["implausible_hire_date"] = False

    hris.to_parquet(PROCESSED / "hris.parquet", index=False)
    acq.to_parquet(PROCESSED / "acquiredco.parquet", index=False)
    payroll.to_parquet(PROCESSED / "payroll.parquet", index=False)
    benefits.to_parquet(PROCESSED / "benefits.parquet", index=False)

    # ---- TRANSFORM: unify, attach payroll, and dedup -----------------
    logger.info("[3/6] Unify & deduplicate")

    employees = pd.concat([hris, acq], ignore_index=True)

    payroll_exclude = {"employee_id", "source", "source_system", "source_systems"}
    benefits_exclude = {"employee_id", "source_system", "source_systems"}
    payroll_columns = [c for c in payroll.columns if c not in payroll_exclude]
    benefits_columns = [c for c in benefits.columns if c not in benefits_exclude]

    payroll = dedup.dedup_id(payroll, priority=source_priority)

    employees = employees.merge(
        payroll[["employee_id", *payroll_columns]],
        on="employee_id",
        how="left",
        suffixes=("", "_payroll"),
    )
    # Provenance: mark payroll contribution
    has_payroll = employees["base_salary"].notna() if "base_salary" in employees.columns else pd.Series(False, index=employees.index)
    employees.loc[has_payroll, "source_systems"] = employees.loc[has_payroll, "source_systems"].astype("string") + ",payroll"

    employees = employees.merge(
        benefits[["employee_id", *benefits_columns]],
        on="employee_id",
        how="left",
        suffixes=("", "_benefits"),
    )
    has_benefits = (
        employees["plan_type"].notna()
        if "plan_type" in employees.columns
        else pd.Series(False, index=employees.index)
    )
    employees.loc[has_benefits, "source_systems"] = (
        employees.loc[has_benefits, "source_systems"].astype("string") + ",benefits"
    )

    employees = clean.to_annual_usd(employees, exchange_rates=rates)

    employees = clean.flag_null_department(employees, REJECTED / "flagged_null_department.csv")
    employees = clean.flag_null_country(employees, REJECTED / "flagged_null_country.csv")

    # Ghost employees: payroll IDs with no corresponding record in the cleaned HRIS set
    # (includes true payroll orphans and employees dropped for null department/country).
    ghost_payroll = dedup.detect_ghost_employees(payroll, employees["employee_id"])
    ghost_payroll = clean.to_annual_usd(ghost_payroll, exchange_rates=rates)
    # Attach names from raw HRIS when available (helps review of filtered-out employees)
    name_lookup = pd.concat([
        hris[["employee_id", "first_name", "last_name"]],
        acq[["employee_id", "first_name", "last_name"]],
    ], ignore_index=True).drop_duplicates("employee_id")
    ghost_payroll = ghost_payroll.merge(name_lookup, on="employee_id", how="left")
    ghost_file = load.write_ghost_employees(ghost_payroll, OUTPUT)
    logger.info("Ghost employee records written: %s (%d rows)", ghost_file, len(ghost_payroll))

    employees = dedup.dedup_id(
        employees,
        priority=source_priority,
        duplicate_output_path=REJECTED / "duplicated_ids.csv",
    )
    employees = dedup.dedup_email(
        employees,
        duplicate_output_path=REJECTED / "duplicated_emails.csv",
    )

    review = dedup.find_fuzzy_candidates(
        employees,
        threshold=dedup_cfg.get("fuzzy_threshold", 88),
        hire_date_window_days=dedup_cfg.get("hire_date_window_days", 30),
    )
    # Tag probable fuzzy matches on the golden set without auto-merging
    if not review.empty:
        fuzzy_ids = set(review["record_1_id"]).union(set(review["record_2_id"]))
        mask = employees["employee_id"].isin(fuzzy_ids) & (employees["dedup_method"] == "single_source")
        employees.loc[mask, "dedup_method"] = "fuzzy_name"

    review.to_parquet(PROCESSED / "fuzzy_review.parquet", index=False)
    employees.to_parquet(PROCESSED / "employees.parquet", index=False)

    # ---- VALIDATE (quality gate) ----------------------------------
    logger.info("[4/6] Validate")
    report = validate.run_validation(
        employees,
        max_failed_checks=gate_cfg.get("max_failed_checks", 2),
        salary_min=salary_bounds.get("min", 15_000),
        salary_max=salary_bounds.get("max", 2_000_000),
    )

    # ---- LOAD ------------------------------------------------------
    logger.info("[5/6] Load")
    golden = load.write_golden_dataset(employees, OUTPUT)
    load.write_review_file(review, OUTPUT)
    load.write_quality_report(report, OUTPUT)

    # ---- GENERATE REPORT ------------------------------------------------------
    logger.info("[6/6] Generate Report")
    try:
        report_png = graph.generate_report(employees, benefits, report, OUTPUT)
        logger.info("Report generated: %s", report_png)
    except Exception as exc:
        logger.warning("Report generation failed: %s", exc)
    logger.info("Done. Golden dataset: %s  (%d employees)", golden, len(employees))
    return 0


def main() -> int:
    logger = logging.getLogger("run_pipeline")
    try:
        return run(cfg)
    except validate.QualityGateError as exc:
        logger.critical("Quality gate failed: %s", exc)
        return 2
    except FileNotFoundError as exc:
        logger.critical("Missing input: %s", exc)
        return 3
    except Exception:
        logger.critical("Unexpected failure", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
