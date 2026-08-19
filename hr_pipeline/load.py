"""Load stage: writes final deliverables to the output zone."""
from pathlib import Path
import shutil
import pandas as pd
import logging

logger = logging.getLogger(__name__)

GHOST_COLUMNS = [
    "payroll_employee_id",
    "name",
    "salary_usd_annual",
    "ghost_flag_reason",
]


def write_golden_dataset(df: pd.DataFrame, output_dir: Path) -> Path:
    """Write unified golden employees dataset partitioned by company_origin.

    Creates a Hive-style directory tree under output/golden_employees.parquet/
    and a flat convenience copy at output/golden_employees_flat.parquet.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "golden_employees.parquet"
    work = df.copy()
    if "company_origin" not in work.columns:
        work["company_origin"] = work["employee_id"].astype("string").str.startswith("GT-").map(
            {True: "GlobalTech", False: "AcquiredCo"}
        )

    if out.exists():
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()

    work.to_parquet(out, index=False, partition_cols=["company_origin"])
    flat = output_dir / "golden_employees_flat.parquet"
    work.to_parquet(flat, index=False)
    logger.info("Wrote partitioned golden dataset to %s (%d rows)", out, len(work))
    return out


def write_review_file(candidates: pd.DataFrame, output_dir: Path) -> Path:
    """Fuzzy-match pairs for HR to confirm or reject."""
    out = output_dir / "probable_matches_review.csv"
    candidates.to_csv(out, index=False)
    return out


def write_quality_report(report: pd.DataFrame, output_dir: Path) -> tuple[Path, Path]:
    """Export quality report as CSV and HTML summary table."""
    csv_path = output_dir / "quality_report.csv"
    html_path = output_dir / "quality_report.html"
    report.to_csv(csv_path, index=False)

    header = "".join(f"<th>{c}</th>" for c in report.columns)
    rows_html = []
    for _, row in report.iterrows():
        status = str(row.get("status", ""))
        cls = ' class="fail"' if status == "FAIL" else ""
        cells = "".join(f"<td>{row[c]}</td>" for c in report.columns)
        rows_html.append(f"<tr{cls}>{cells}</tr>")

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>HR Pipeline Data Quality Report</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 2rem; color: #222; }}
    h1 {{ font-size: 1.4rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 0.45rem 0.6rem; text-align: left; }}
    th {{ background: #f0f4f8; }}
    tr.fail td {{ background: #fff5f5; }}
  </style>
</head>
<body>
  <h1>HR Pipeline Data Quality Report</h1>
  <p>Generated for GlobalTech / AcquiredCo integration pipeline.</p>
  <table>
    <thead><tr>{header}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</body>
</html>
"""
    html_path.write_text(html_doc, encoding="utf-8")
    logger.info("Wrote quality report CSV=%s HTML=%s", csv_path, html_path)
    return csv_path, html_path


def write_ghost_employees(df: pd.DataFrame, output_dir: Path) -> Path:
    """Write ghost payroll records with the required review schema."""
    out = output_dir / "ghost_employees.csv"
    work = df.copy()

    if "payroll_employee_id" not in work.columns:
        work["payroll_employee_id"] = work.get("employee_id")
    if "name" not in work.columns:
        if {"first_name", "last_name"}.issubset(work.columns):
            work["name"] = (
                work["first_name"].astype("string").fillna("")
                + " "
                + work["last_name"].astype("string").fillna("")
            ).str.strip()
        else:
            work["name"] = pd.NA
    if "salary_usd_annual" not in work.columns:
        work["salary_usd_annual"] = pd.NA
    if "ghost_flag_reason" not in work.columns:
        work["ghost_flag_reason"] = "Payroll record with no matching HRIS employee"

    export = work.reindex(columns=GHOST_COLUMNS)
    export.to_csv(out, index=False)
    logger.info("Wrote ghost employees: %s (%d rows)", out, len(export))
    return out


def atomic_write_parquet(df: pd.DataFrame, target: Path) -> Path:
    """Write to a temp file, then rename — readers never see a half-written file."""
    tmp = target.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(target)
    return target
