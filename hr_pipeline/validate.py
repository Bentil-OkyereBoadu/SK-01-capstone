"""Rule-based data quality validation with a pass/fail report."""
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class QualityGateError(RuntimeError):
    """Raised when the quality gate fails too many validation checks."""


class DataQualityValidator:
    def __init__(self, df: pd.DataFrame, name: str):
        self.df, self.name, self.results = df, name, []

    def _record(self, check: str, description: str, failed_mask: pd.Series):
        failed = int(failed_mask.sum())
        total = len(self.df)
        self.results.append({
            "check": check, "description": description,
            "total": total, "passed": total - failed, "failed": failed,
            "pass_rate": round(100 * (total - failed) / total, 2) if total else 100.0,
            "status": "PASS" if failed == 0 else "FAIL",
        })

    def not_null(self, col: str):
        self._record(f"not_null:{col}", f"{col} must be present", self.df[col].isna())
        return self

    def unique(self, col: str):
        self._record(f"unique:{col}", f"{col} must be unique",
                     self.df[col].duplicated(keep=False))
        return self

    def in_set(self, col: str, allowed: set):
        self._record(f"in_set:{col}", f"{col} in {sorted(allowed)}",
                     ~self.df[col].isin(allowed) & self.df[col].notna())
        return self

    def matches(self, col: str, pattern: str):
        ok = self.df[col].astype("string").str.match(pattern, na=False)
        self._record(f"regex:{col}", f"{col} matches {pattern}", ~ok)
        return self

    def in_range(self, col: str, lo, hi):
        s = self.df[col]
        self._record(f"range:{col}", f"{lo} <= {col} <= {hi}",
                     ((s < lo) | (s > hi)) & s.notna())
        return self

    def date_in_range(self, col: str, start="1970-01-01", end=None):
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end) if end is not None else pd.Timestamp.today().normalize()
        s = pd.to_datetime(self.df[col], errors="coerce")
        self._record(
            f"date_range:{col}",
            f"{col} between {start_ts.date()} and {end_ts.date()}",
            ((s < start_ts) | (s > end_ts) | s.isna()) & self.df[col].notna()
        )
        return self

    def foreign_key_exists(self, col: str, reference_col: str):
        missing_manager = self.df[col].notna() & ~self.df[col].isin(self.df[reference_col])
        self._record(
            f"foreign_key:{col}->{reference_col}",
            f"Every non-null {col} must exist in {reference_col}",
            missing_manager
        )
        return self

    def report(self) -> pd.DataFrame:
        return pd.DataFrame(self.results)


def run_validation(
    df: pd.DataFrame,
    max_failed_checks: int = 2,
    salary_min: float = 15_000,
    salary_max: float = 2_000_000,
) -> pd.DataFrame:
    """Run the required quality checks and enforce the pipeline gate."""
    v = (DataQualityValidator(df, "employees")
         .not_null("employee_id")
         .not_null("email")
         .not_null("first_name")
         .not_null("last_name")
         .not_null("department")
         .not_null("country")
         .unique("employee_id")
         .unique("email")
         .in_set("employment_type", {"Full-Time", "Part-Time", "Contractor"})
         .in_set("currency", {"USD", "EUR", "GBP"})
         .matches("employee_id", r"^(GT|AC)-\d{6}$")
         .matches("email", r"^[\w.+-]+@[\w-]+\.[\w.]+$")
         .in_range("salary_usd_annual", salary_min, salary_max)
         .date_in_range("hire_date", start="1970-01-01", end=None)
         .foreign_key_exists("manager_id", "employee_id")
         )
    report = v.report()
    failures = int((report["status"] == "FAIL").sum())
    logger.info(report.to_string(index=False))
    if failures > max_failed_checks:
        raise QualityGateError(
            f"QUALITY GATE FAILED: {failures} checks failing "
            f"(max allowed {max_failed_checks}) — halting pipeline"
        )
    return report
