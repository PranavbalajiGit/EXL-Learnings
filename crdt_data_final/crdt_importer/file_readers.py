from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


PREDICTION_REQUIRED_COLUMNS = {
    "run_id",
    "pipeline_version",
    "model_name",
    "prompt_version",
    "category_version",
    "audit_id",
    "code",
    "analysis",
    "uhdds_analysis",
    "findings",
    "category",
    "finding_outcome",
    "rationale",
}

FEEDBACK_REQUIRED_COLUMNS = {
    "Audit ID",
    "Code",
    "Auditor Name",
    "Model Outcome",
    "Auditor Comments",
    "AI Analysis Gap",
    "Diagnosis Type",
    "Insights",
    "Auditor Category",
}


def _validate_columns(actual_columns: list[Any], required: set[str], label: str) -> None:
    actual = {str(column).strip() for column in actual_columns}
    missing = sorted(required - actual)
    if missing:
        raise ValueError(
            f"{label} file is missing required column(s): {', '.join(missing)}. "
            f"Expected exact standardized headers; no alias mapping is performed."
        )


def _normalize_text(value: Any) -> str | None:
    if pd.isna(value):
        return None
    result = str(value).strip()
    return result if result else None


def _normalize_integer(value: Any, field: str, row_number: int) -> int:
    if pd.isna(value):
        raise ValueError(f"{field} is empty at data row {row_number}.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer at data row {row_number}; got {value!r}.") from exc
    if not numeric.is_integer():
        raise ValueError(f"{field} must be an integer at data row {row_number}; got {value!r}.")
    return int(numeric)


def _normalize_boolean(value: Any, field: str, row_number: int) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        raise ValueError(f"{field} is empty at data row {row_number}.")
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "yes", "y", "1"}:
        return True
    if normalized in {"false", "f", "no", "n", "0"}:
        return False
    raise ValueError(
        f"{field} must be a boolean (true/false or 1/0) at data row {row_number}; got {value!r}."
    )


def _normalize_code(value: Any, row_number: int, field: str = "code") -> str:
    code = _normalize_text(value)
    if code is None:
        raise ValueError(f"{field} is empty at data row {row_number}.")
    return code


def read_predictions_csv(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    df = pd.read_csv(path, dtype={"audit_id": "string", "code": "string"})
    _validate_columns(list(df.columns), PREDICTION_REQUIRED_COLUMNS, "Prediction CSV")

    records: list[dict[str, Any]] = []
    metadata_fields = ("run_id", "pipeline_version", "model_name", "prompt_version", "category_version")
    metadata: dict[str, Any] | None = None

    for idx, row in df.iterrows():
        row_number = int(idx) + 2  # include header row in CSV line number
        row_metadata = {
            "run_id": _normalize_integer(row["run_id"], "run_id", row_number),
            "pipeline_version": _normalize_text(row["pipeline_version"]),
            "model_name": _normalize_text(row["model_name"]),
            "prompt_version": _normalize_text(row["prompt_version"]),
            "category_version": _normalize_text(row["category_version"]),
        }
        if metadata is None:
            metadata = row_metadata
        elif any(row_metadata[key] != metadata[key] for key in metadata_fields):
            raise ValueError(
                f"Prediction metadata differs at CSV line {row_number}. "
                "run_id and pipeline metadata must be consistent across all rows."
            )

        audit_id = _normalize_integer(row["audit_id"], "audit_id", row_number)
        code = _normalize_code(row["code"], row_number)
        record = {
            **row_metadata,
            "audit_id": audit_id,
            "code": code,
            "analysis": _normalize_text(row["analysis"]),
            "uhdds_analysis": _normalize_text(row["uhdds_analysis"]),
            "findings": _normalize_text(row["findings"]),
            "category": _normalize_text(row["category"]),
            "finding_outcome": _normalize_boolean(row["finding_outcome"], "finding_outcome", row_number),
            "rationale": _normalize_text(row["rationale"]),
        }
        records.append(record)

    if not records:
        raise ValueError("Prediction CSV contains no data rows; refusing to update pipeline_runs with an empty run.")

    assert metadata is not None
    return records, metadata


def read_feedback_excel(path: str | Path) -> list[dict[str, Any]]:
    df = pd.read_excel(path, dtype={"Audit ID": "string", "Code": "string"})
    _validate_columns(list(df.columns), FEEDBACK_REQUIRED_COLUMNS, "Auditor feedback Excel")

    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        row_number = int(idx) + 2  # Excel row number including header
        audit_id = _normalize_integer(row["Audit ID"], "Audit ID", row_number)
        code = _normalize_code(row["Code"], row_number, "Code")
        model_outcome = _normalize_text(row["Model Outcome"])
        if model_outcome is None:
            raise ValueError(f"Model Outcome is empty at Excel row {row_number}.")
        normalized_outcome = model_outcome.strip().title()
        if normalized_outcome not in {"Correct", "Incorrect"}:
            raise ValueError(
                f"Model Outcome must be 'Correct' or 'Incorrect' at Excel row {row_number}; "
                f"got {model_outcome!r}."
            )

        records.append({
            "audit_id": audit_id,
            "code": code,
            "auditor_name": _normalize_text(row["Auditor Name"]),
            "model_outcome": normalized_outcome,
            "auditor_comments": _normalize_text(row["Auditor Comments"]),
            "ai_analysis_gap": _normalize_text(row["AI Analysis Gap"]),
            "diagnosis_type": _normalize_text(row["Diagnosis Type"]),
            "insights": _normalize_text(row["Insights"]),
            "auditor_category": _normalize_text(row["Auditor Category"]),
            "source_row": row_number,
        })
    return records
