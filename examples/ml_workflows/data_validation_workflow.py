"""
Data Quality Validation Pipeline using Great Expectations with Flyte example.

This script demonstrates:
- Schema validation
- Statistical expectation checks
- Simple data drift detection across runs
- Automated alerting via logging and (optional) webhook
- Robust error handling and structured logging

It is designed to be runnable as a standalone Python module and integratable
within a Flyte task/workflow. Dependencies:
  - pandas>=1.5
  - great-expectations>=0.18
  - requests (optional, for webhook alerting)

Note: This script does not require an on-disk Great Expectations project; it
uses in-memory DataContext and ExpectationSuite creation for simplicity.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

try:
    import great_expectations as ge
    from great_expectations.core import ExpectationSuiteValidationResult
    from great_expectations.data_context import BaseDataContext
    from great_expectations.expectations.core import (
        ExpectColumnValuesToNotBeNull,
        ExpectTableRowCountToBeBetween,
    )
    from great_expectations.datasource.fluent import PandasDatasource
    from great_expectations.validator.validator import Validator
except Exception as e:  # pragma: no cover - import-time diagnostics
    raise RuntimeError(
        "Great Expectations is required. Please install great-expectations >= 0.18"
    ) from e

try:
    import requests  # optional for webhook alerts
except Exception:
    requests = None  # type: ignore


# --------------------------- Logging Configuration ---------------------------
LOG_LEVEL = os.environ.get("DV_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("data_validation_workflow")


# ------------------------------ Data Contracts ------------------------------
@dataclass
class DriftConfig:
    reference_stats_path: str = "./artifacts/reference_stats.json"
    drift_threshold_pct: float = 0.15  # 15% relative change


@dataclass
class AlertConfig:
    enable_webhook: bool = False
    webhook_url: Optional[str] = os.environ.get("DV_WEBHOOK_URL")
    timeout_sec: int = 8


# ---------------------------- Utility Functions -----------------------------

def send_alert(message: str, level: str = "ERROR", alert_cfg: Optional[AlertConfig] = None) -> None:
    """Send alert to logger and optional webhook.

    Args:
        message: Alert message content
        level: One of "INFO", "WARNING", "ERROR"
        alert_cfg: Alert configuration
    """
    level = level.upper()
    log_fn = getattr(logger, level.lower(), logger.error)
    log_fn("ALERT: %s", message)

    cfg = alert_cfg or AlertConfig()
    if cfg.enable_webhook and cfg.webhook_url and requests is not None:
        try:
            resp = requests.post(
                cfg.webhook_url,
                json={
                    "timestamp": datetime.utcnow().isoformat(),
                    "level": level,
                    "message": message,
                    "source": "data_validation_workflow",
                },
                timeout=cfg.timeout_sec,
            )
            resp.raise_for_status()
            logger.info("Alert webhook delivered (status %s)", resp.status_code)
        except Exception as e:
            logger.warning("Failed to deliver alert webhook: %s", e)


def load_dataframe(source: str) -> pd.DataFrame:
    """Load data from a CSV file path or URL into a DataFrame.

    For demo purposes, if the source does not exist, we generate a sample DF.
    """
    if source and os.path.exists(source):
        logger.info("Loading CSV from %s", source)
        return pd.read_csv(source)

    if source and source.startswith("http"):
        logger.info("Loading CSV from URL %s", source)
        return pd.read_csv(source)

    logger.warning("Source not found; generating sample dataset in-memory")
    data = {
        "id": [1, 2, 3, 4, 5, 6],
        "age": [25, 40, 36, 29, 52, None],
        "country": ["US", "US", "CA", "IN", "DE", "US"],
        "income": [55000, 82000, 61000, 72000, 90000, 58000],
        "signup_date": [
            "2023-01-03",
            "2023-02-10",
            "2023-03-21",
            "2023-03-29",
            "2023-04-01",
            "2023-04-11",
        ],
    }
    df = pd.DataFrame(data)
    df["signup_date"] = pd.to_datetime(df["signup_date"])  # type: ignore
    return df


# ------------------------ Great Expectations Setup -------------------------

def build_in_memory_context() -> BaseDataContext:
    """Create an in-memory DataContext with a Pandas datasource."""
    context = BaseDataContext(project_config={})

    # Add a fluent-style datasource
    # GE 0.18+ fluent API
    ds = PandasDatasource(name="pandas_src")
    context.fluent_datasources[ds.name] = ds
    return context


def create_expectation_suite(context: BaseDataContext, suite_name: str = "dq_suite") -> None:
    """Create or overwrite an expectation suite with sample expectations."""
    context.add_or_update_expectation_suite(suite_name)

    # Use batch validator (we will attach expectations dynamically during validation)
    logger.info("Initialized expectation suite '%s'", suite_name)


def validate_with_expectations(
    context: BaseDataContext,
    df: pd.DataFrame,
    suite_name: str = "dq_suite",
) -> ExpectationSuiteValidationResult:
    """Attach expectations and validate the dataframe."""
    ge_df = ge.from_pandas(df)
    validator: Validator = context.get_validator(
        batch_request=None,  # not using batch requests for simple flow
        expectation_suite_name=suite_name,
        datasource_name=None,
        data_asset_name="in_memory_df",
        batch_data=ge_df,
    )

    # Schema/Nullability expectations
    validator.expect_table_row_count_to_be_between(min_value=1)
    for col in ["id", "age", "country", "income", "signup_date"]:
        validator.expect_column_to_exist(col)

    validator.expect_column_values_to_not_be_null("id")
    validator.expect_column_values_to_be_unique("id")

    # Type/statistical expectations
    validator.expect_column_values_to_be_of_type("id", "int64")
    validator.expect_column_min_to_be_between("age", min_value=0)
    validator.expect_column_values_to_not_be_null("income")
    validator.expect_column_mean_to_be_between("income", min_value=10000, max_value=200000)

    # Categorical domain
    validator.expect_column_values_to_be_in_set("country", ["US", "CA", "IN", "DE", "UK", "FR"])  # allow unseen but constrained

    # Date constraints
    validator.expect_column_values_to_match_strftime_format("signup_date", "%Y-%m-%d")

    # Run validation
    results: ExpectationSuiteValidationResult = validator.validate()
    return results


# ---------------------------- Drift Detection -------------------------------

def compute_basic_stats(df: pd.DataFrame) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    numeric_cols = df.select_dtypes(include=["number"]).columns
    for col in numeric_cols:
        series = df[col].dropna()
        if series.empty:
            continue
        stats[col] = {
            "count": int(series.count()),
            "mean": float(series.mean()),
            "std": float(series.std() if series.count() > 1 else 0.0),
            "min": float(series.min()),
            "max": float(series.max()),
        }
    return stats


def detect_drift(current: Dict[str, Any], reference: Optional[Dict[str, Any]], cfg: DriftConfig) -> Dict[str, Any]:
    """Compare current stats with reference; flag drift if relative change exceeds threshold."""
    drift_report = {"drifted": False, "details": {}}
    if not reference:
        return drift_report

    thr = cfg.drift_threshold_pct
    for col, cur in current.items():
        ref = reference.get(col)
        if not ref:
            continue
        details = {}
        for metric in ["mean", "std", "min", "max"]:
            c = cur.get(metric)
            r = ref.get(metric)
            if r is None or c is None:
                continue
            denom = abs(r) if r != 0 else 1.0
            rel = abs(c - r) / denom
            if rel > thr:
                drift_report["drifted"] = True
                details[metric] = {"current": c, "reference": r, "relative_change": rel}
        if details:
            drift_report["details"][col] = details
    return drift_report


def save_reference_stats(stats: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.utcnow().isoformat(), "stats": stats}, f, indent=2)


def load_reference_stats(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload.get("stats")
    except Exception as e:
        logger.warning("Failed to load reference stats from %s: %s", path, e)
        return None


# ------------------------------ Main Routine --------------------------------

def run_validation(
    source: str = os.environ.get("DV_SOURCE", ""),
    suite_name: str = "dq_suite",
    drift_cfg: DriftConfig = DriftConfig(),
    alert_cfg: AlertConfig = AlertConfig(),
) -> int:
    """Execute validation pipeline; returns process exit code (0=success, 1=failure)."""
    try:
        df = load_dataframe(source)
    except Exception as e:
        send_alert(f"Failed to load dataset: {e}", level="ERROR", alert_cfg=alert_cfg)
        return 1

    # Build GE context and run validation
    try:
        context = build_in_memory_context()
        create_expectation_suite(context, suite_name)
        results = validate_with_expectations(context, df, suite_name)
    except Exception as e:
        send_alert(f"Validation execution error: {e}", level="ERROR", alert_cfg=alert_cfg)
        return 1

    success = bool(getattr(results, "success", False))
    summary = {
        "success": success,
        "statistics": getattr(results, "statistics", {}),
    }
    logger.info("Validation summary: %s", json.dumps(summary))

    # Drift detection
    try:
        current_stats = compute_basic_stats(df)
        reference_stats = load_reference_stats(drift_cfg.reference_stats_path)
        drift_report = detect_drift(current_stats, reference_stats, drift_cfg)
        logger.info("Drift report: %s", json.dumps(drift_report))
        save_reference_stats(current_stats, drift_cfg.reference_stats_path)
    except Exception as e:
        logger.warning("Drift detection failed: %s", e)
        drift_report = {"drifted": False, "details": {}}

    # Alerting logic
    if not success:
        send_alert("Data validation failed. See logs for details.", level="ERROR", alert_cfg=alert_cfg)
    elif drift_report.get("drifted"):
        send_alert("Data drift detected beyond threshold.", level="WARNING", alert_cfg=alert_cfg)
    else:
        logger.info("Data validation passed and no drift detected.")

    # Emit JSON artifact (optional)
    try:
        os.makedirs("./artifacts", exist_ok=True)
        with open("./artifacts/validation_summary.json", "w", encoding="utf-8") as f:
            json.dump({"validation": summary, "drift": drift_report}, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write validation summary artifact: %s", e)

    return 0 if success else 1


# --------------------------- Flyte Task Integration --------------------------
try:
    # Lazy import so script can run without Flytekit installed
    from flytekit import task, workflow

    @task
    def validate_task(source: str = "") -> int:
        return run_validation(source=source)

    @workflow
    def validation_workflow(source: str = "") -> int:
        return validate_task(source=source)

except Exception:  # Flyte not installed; expose CLI only
    validation_workflow = None  # type: ignore


# ---------------------------------- CLI -------------------------------------
if __name__ == "__main__":
    src = os.environ.get("DV_SOURCE", "")
    exit_code = run_validation(source=src)
    sys.exit(exit_code)
