"""Visualization report generator for HR pipeline.

Generates a 3x2 figure with six charts and saves a high-resolution PNG.
"""
from pathlib import Path
from datetime import datetime, timezone
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np


def _ensure_output_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _annotate_source(ax, text: str):
    """Add a data-source annotation in the lower-left of each chart."""
    ax.text(
        0.01, -0.18, text,
        transform=ax.transAxes,
        fontsize=7,
        color="#555555",
        ha="left",
        va="top",
        wrap=True,
    )


def chart_headcount_by_department(ax, employees, palette):
    by_dep = employees["department"].fillna("(Unknown)")
    counts = by_dep.value_counts().sort_values()
    ax.barh(counts.index, counts.values, color=palette[0])
    ax.set_title("Headcount by Department")
    ax.set_xlabel("Employees")
    ax.set_ylabel("Department")
    _annotate_source(ax, "Source: golden employee dataset (HRIS + AcquiredCo)")


def chart_headcount_by_country(ax, employees, palette):
    by_country = employees["country"].fillna("(Unknown)")
    counts = by_country.value_counts().nlargest(20)
    ax.bar(counts.index, counts.values, color=palette[1])
    ax.set_title("Headcount by Country")
    ax.set_xlabel("Country")
    ax.set_ylabel("Employees")
    ax.set_xticks(np.arange(len(counts)))
    ax.set_xticklabels(counts.index, rotation=45, ha="right")
    _annotate_source(ax, "Source: golden employee dataset (country field)")


def chart_salary_by_employment_type(ax, employees, palette):
    df = employees[["employment_type", "salary_usd_annual"]].dropna()
    if df.empty:
        ax.text(0.5, 0.5, "No salary data available", ha="center", va="center")
        ax.set_axis_off()
        return
    sns.boxplot(x="employment_type", y="salary_usd_annual", data=df, color=palette[2], ax=ax)
    ax.set_title("Salary Distribution by Employment Type")
    ax.set_xlabel("Employment Type")
    ax.set_ylabel("Annual Salary (USD)")
    _annotate_source(ax, "Source: payroll joined to HRIS (salary_usd_annual)")


def chart_tenure_histogram(ax, employees, palette):
    hire = pd.to_datetime(employees.get("hire_date", pd.Series(dtype="datetime64[ns]")), errors="coerce")
    now = pd.Timestamp.today()
    tenure_years = ((now - hire).dt.days / 365.25).dropna()
    if tenure_years.empty:
        ax.text(0.5, 0.5, "No hire date data available", ha="center", va="center")
        ax.set_axis_off()
        return
    ax.hist(tenure_years, bins=30, color=palette[3])
    ax.set_title("Tenure Distribution")
    ax.set_xlabel("Tenure (years)")
    ax.set_ylabel("Employees")
    _annotate_source(ax, "Source: hire_date from GlobalTech / AcquiredCo HRIS")


def chart_benefits_enrollment_by_department(ax, employees, benefits, palette):
    emp_digits = employees["employee_id"].astype("string").str.extract(r"(\d+)")[0]
    benefits_ids = pd.Series(benefits["employee_id"].astype("string")).str.extract(r"(\d+)")[0]
    enrolled_set = set(benefits_ids.dropna().unique())
    dept = employees["department"].fillna("(Unknown)")
    enrolled = emp_digits.isin(enrolled_set)
    df = pd.DataFrame({"department": dept, "enrolled": enrolled})
    summary = (
        df.groupby("department")["enrolled"]
        .agg(["sum", "size"])
        .rename(columns={"sum": "enrolled_count", "size": "total"})
    )
    summary["enrollment_rate"] = summary["enrolled_count"] / summary["total"]
    summary = summary.sort_values("enrollment_rate", ascending=False).head(20)
    ax.bar(np.arange(len(summary)), summary["enrollment_rate"], color=palette[4])
    ax.set_title("Benefits Enrollment Rate by Department")
    ax.set_xlabel("Department")
    ax.set_ylabel("Enrollment Rate")
    ax.set_xticks(np.arange(len(summary)))
    ax.set_xticklabels(summary.index, rotation=45, ha="right")
    _annotate_source(ax, "Source: benefits_enrollment.xml joined to employees")


def chart_data_quality_summary(ax, quality_report, palette):
    if quality_report is None or quality_report.empty:
        ax.text(0.5, 0.5, "No quality report available", ha="center", va="center")
        ax.set_axis_off()
        return
    df = quality_report.copy()
    checks = df["check"].astype(str)
    passed = df["passed"].fillna(0).astype(int)
    failed = df["failed"].fillna(0).astype(int)
    x = np.arange(len(checks))
    width = 0.35
    ax.bar(x - width / 2, passed, width, label="Passed", color=palette[0])
    ax.bar(x + width / 2, failed, width, label="Failed", color=palette[5])
    ax.set_xticks(x)
    ax.set_xticklabels(checks, rotation=45, ha="right")
    ax.set_title("Data Quality Summary")
    ax.set_xlabel("Check")
    ax.set_ylabel("Rows")
    ax.legend()
    _annotate_source(ax, "Source: validate.py DataQualityValidator report")


def generate_report(
    employees: pd.DataFrame,
    benefits: pd.DataFrame,
    quality_report: pd.DataFrame,
    output_dir: Path,
) -> Path:
    """Generate the PNG report with six charts and return the saved Path."""
    _ensure_output_dir(output_dir)
    sns.set_style("whitegrid")
    palette = sns.color_palette("colorblind", 8)

    fig, axs = plt.subplots(3, 2, figsize=(14, 18))
    axs = axs.flatten()

    chart_headcount_by_department(axs[0], employees, palette)
    chart_headcount_by_country(axs[1], employees, palette)
    chart_salary_by_employment_type(axs[2], employees, palette)
    chart_tenure_histogram(axs[3], employees, palette)
    chart_benefits_enrollment_by_department(axs[4], employees, benefits, palette)
    chart_data_quality_summary(axs[5], quality_report, palette)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.suptitle("HR Visualization Report — GlobalTech / AcquiredCo Integration", fontsize=18)
    fig.text(0.5, 0.955, f"Generated: {timestamp}", ha="center", fontsize=9)

    plt.tight_layout(rect=[0, 0.02, 1, 0.94])
    out = output_dir / "hr_report.png"
    fig.savefig(out, dpi=300)
    plt.close(fig)
    return out
