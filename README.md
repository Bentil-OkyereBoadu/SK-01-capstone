# SK-01 Capstone: Multi-Source HR Data Integration Pipeline

Unified employee data pipeline for **GlobalTech Corp** after the acquisition of **AcquiredCo**. The pipeline ingests four heterogeneous HR sources, cleans and standardizes them, deduplicates with provenance tracking, validates data quality, and publishes a golden employee dataset plus compliance review files.

## Business context

HR leadership needs a unified employee dataset within 10 business days to support:

- Day 1 integration planning (who is joining, in which role, at what cost)
- Benefits enrollment eligibility verification
- Payroll system migration
- Compliance reporting (headcount by jurisdiction, salary band distribution)

## Input sources

| Source | Path | Format | Notes |
|--------|------|--------|-------|
| GlobalTech HRIS | `data/raw/globaltech_hris.csv` | CSV (UTF-8) | Workday export; department names/codes vary by business unit |
| AcquiredCo HRIS | `data/raw/acquiredco_api.json` | JSON | BambooHR-style dump; ingested with **simulated API pagination** |
| Combined Payroll | `data/raw/payroll_data.xlsx` | Excel | Mixed currencies (USD/EUR/GBP); possible duplicates |
| Benefits Provider | `data/raw/benefits_enrollment.xml` | XML | MedShield; GlobalTech employees only |

Configuration lives in [`config/pipeline.yaml`](config/pipeline.yaml) (exchange rates, quality gate, fuzzy threshold, hire-date window).

## Pipeline modules

| Module | Role |
|--------|------|
| `hr_pipeline/ingest.py` | Four source loaders, schema alignment, pagination simulation, dead-letter logging |
| `hr_pipeline/clean.py` | Name/ID/currency/department/date standardization |
| `hr_pipeline/dedup.py` | Exact ID (source priority), email match, fuzzy review, ghost detection |
| `hr_pipeline/validate.py` | `DataQualityValidator` with 15 checks and quality gate |
| `hr_pipeline/load.py` | Golden parquet, ghost CSV, review CSV, quality CSV+HTML |
| `hr_pipeline/graph.py` | Six-chart 300 DPI visualization report |
| `run_pipeline.py` | End-to-end orchestration |

## How to run

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python run_pipeline.py
```

Exit codes: `0` success, `2` quality gate failure, `3` missing input, `1` unexpected error.

## Output files

| Path | Format | Description |
|------|--------|-------------|
| `output/golden_employees.parquet/` | Parquet (partitioned by `company_origin`) | Unified, cleaned, deduplicated employees |
| `output/golden_employees_flat.parquet` | Parquet | Flat convenience copy of the golden set |
| `output/ghost_employees.csv` | CSV | Payroll records with no HRIS match |
| `output/probable_matches_review.csv` | CSV | Fuzzy name matches for HR review |
| `output/quality_report.csv` | CSV | Validation check results |
| `output/quality_report.html` | HTML | Human-readable quality summary table |
| `output/hr_report.png` | PNG (300 DPI) | Six-chart EDA / visualization report |
| `data/rejected/dead_letter.csv` | CSV | Malformed / failed ingest records |
| `data/rejected/flagged_null_*.csv` | CSV | Rows removed for missing department/country |
| `logs/pipeline_*.log` | Log | Timestamped run logs |

### Ghost employee CSV schema

| Column | Description |
|--------|-------------|
| `payroll_employee_id` | Namespaced payroll employee ID |
| `name` | Employee name when available (often null for payroll-only ghosts) |
| `salary_usd_annual` | Annualized USD salary |
| `ghost_flag_reason` | Why the record was flagged |

### Probable match review CSV schema

| Column | Description |
|--------|-------------|
| `record_1_id` | First employee ID in the pair |
| `record_2_id` | Second employee ID in the pair |
| `similarity_score` | RapidFuzz name similarity (0–100) |
| `hire_date_diff_days` | Signed hire-date difference in days |
| `recommended_action` | Always `HR review` (no auto-merge) |

## Golden dataset schema

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| `employee_id` | string | Namespaced ID (`GT-######` or `AC-######`) | `GT-001042` |
| `first_name` | string | Unicode-normalized, title-cased given name | `José` |
| `last_name` | string | Title-cased surname (hyphen / particle aware) | `O'Brien` |
| `email` | string | Work email | `jose.obrien@globaltech.com` |
| `department` | string | Unified department taxonomy | `Engineering` |
| `role` | string | Job title / role | `Data Analyst` |
| `hire_date` | datetime64[ns] | Normalized hire date | `2019-03-15` |
| `country` | string | Work location / jurisdiction | `Netherlands` |
| `employment_type` | string | `Full-Time` / `Part-Time` / `Contractor` | `Full-Time` |
| `manager_id` | string | Namespaced manager employee ID | `GT-012765` |
| `company_origin` | string | Partition key: `GlobalTech` or `AcquiredCo` | `GlobalTech` |
| `source_systems` | string | Comma-separated contributing sources | `globaltech_hris,payroll,benefits` |
| `dedup_method` | string | `exact_id` / `email_match` / `fuzzy_name` / `single_source` | `single_source` |
| `base_salary` | float | Original payroll amount | `85000.0` |
| `currency` | string | Original currency code | `EUR` |
| `pay_frequency` | string | Original pay frequency | `Monthly` |
| `salary_usd_annual` | float | Annualized salary in USD | `110160.0` |
| `plan_type` | string | Benefits plan (if enrolled) | `PPO` |
| `coverage_level` | string | Benefits coverage level | `Employee+Family` |
| `enrollment_date` | datetime64[ns] | Benefits enrollment date | `2022-01-15` |

## Deduplication summary

1. **Exact ID** — same namespaced ID; source priority HRIS > Payroll > Benefits
2. **Email** — identical email across companies treated as the same person
3. **Fuzzy name** — RapidFuzz ≥ 88% with hire dates within 30 days; **review only** (not auto-merged)
4. **Ghost employees** — payroll IDs absent from HRIS written to a separate compliance file

## Known limitations and assumptions

- Exchange rates are fixed in `config/pipeline.yaml` (not live FX feeds).
- Benefits cover GlobalTech employees only; AcquiredCo enrollment is out of scope for this feed.
- Fuzzy matches are flagged for HR review and are not automatically merged into the golden set.
- Ghost payroll records often lack names because the payroll extract has no name fields.
- Manager referential integrity may fail when managers are outside the delivered extract or were filtered for null department/country.
- Some salaries fall outside the $15k–$2M validation band (data quality issue surfaced by the gate, not silently fixed).
- Department codes such as `ENG-01` are mapped via `DEPARTMENT_MAP`; unmapped values are logged for manual review.
- Simulated AcquiredCo pagination reads from a single JSON file in page-sized chunks; it is not a live HTTP API.

## Change log

| Date | Change |
|------|--------|
| 2026-08-17 | Compliance pass: provenance columns, partitioned golden parquet, HTML quality report, schema alignment, pagination simulation, dead-letter logging, name/department standardization, source-priority dedup, 30-day fuzzy window, config wiring, README |
| 2026-08-12 | Initial multi-source pipeline (ingest → clean → dedup → validate → load → report) |
