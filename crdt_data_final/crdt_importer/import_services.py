from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .normalization import normalize_code_python
from .models import AuditCode, AuditorReview, GroundTruth, OpinionPrediction, PipelineRun


PREDICTION_UPDATE_FIELDS = (
    "analysis",
    "uhdds_analysis",
    "findings",
    "category",
    "finding_outcome",
    "rationale",
)
REVIEW_UPDATE_FIELDS = (
    "auditor_name",
    "model_outcome",
    "auditor_comments",
    "ai_analysis_gap",
    "diagnosis_type",
    "insights",
    "auditor_category",
)


def _chunks(items: list[Any], size: int = 500):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _pair_key(audit_id: int, code: str) -> tuple[int, str]:
    return audit_id, normalize_code_python(code)


def _get_or_create_audit_codes(session: Session, pairs: set[tuple[int, str]]) -> dict[tuple[int, str], AuditCode]:
    """
    Reuse existing AuditCode rows or insert missing ones.
    The database generates code_norm, so code_norm is deliberately not assigned.
    `pairs` contains (audit_id, normalized_code); a normalized code is used as
    the stable lookup key. The original code string is retained on insert.
    """
    result: dict[tuple[int, str], AuditCode] = {}
    if not pairs:
        return result

    audit_ids = sorted({audit_id for audit_id, _ in pairs})
    # Load existing rows for these audits, then compare normalized values in Python.
    for ids in _chunks(audit_ids):
        rows = session.scalars(
            select(AuditCode).where(AuditCode.audit_id.in_(ids))
        ).all()
        for row in rows:
            key = (int(row.audit_id), str(row.code_norm))
            if key in pairs:
                result[key] = row

    missing = pairs - result.keys()
    for audit_id, code_norm in sorted(missing):
        # The database's generated code_norm removes punctuation and uppercases.
        # Store the normalized form as code; this guarantees the generated value
        # equals the requested normalized key even when source spelling differs.
        row = AuditCode(audit_id=audit_id, code=code_norm)
        session.add(row)
        result[(audit_id, code_norm)] = row

    session.flush()
    # Refresh generated code_norm values after flush.
    for key, row in list(result.items()):
        if row.code_norm is None:
            session.refresh(row)
        actual_key = (int(row.audit_id), str(row.code_norm))
        if actual_key != key:
            raise ValueError(
                f"Database generated code_norm {actual_key[1]!r} does not match "
                f"the expected normalized key {key[1]!r}. Verify the generated column expression."
            )
    return result


def _update_fields(obj: Any, record: dict[str, Any], fields: tuple[str, ...]) -> bool:
    changed = False
    for field in fields:
        new_value = record.get(field)
        if getattr(obj, field) != new_value:
            setattr(obj, field, new_value)
            changed = True
    return changed


def _write_missing_feedback_csv(
    missing: list[dict[str, Any]], report_dir: str | Path, run_id: int
) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"missing_feedback_pairs_run_{run_id}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["run_id", "audit_id", "code"])
        writer.writeheader()
        writer.writerows(missing)
    return path


def import_predictions_and_codes(
    session: Session,
    prediction_records: list[dict[str, Any]],
    metadata: dict[str, Any],
    logger,
    report_dir: str | Path,
) -> dict[str, Any]:
    """
    Upsert pipeline_runs, audit_codes, and opinion_predictions.

    Existing prediction rows are updated in place. Rows absent from the input
    are not deleted. The caller owns the transaction and should commit only
    after the full import workflow has succeeded.
    """
    run_id = int(metadata["run_id"])
    audit_ids = sorted({int(row["audit_id"]) for row in prediction_records})
    if not audit_ids:
        raise ValueError("No prediction audit IDs were supplied.")

    # Pipeline metadata is updated from the latest file, per the agreed behavior.
    pipeline = session.get(PipelineRun, run_id)
    if pipeline is None:
        pipeline = PipelineRun(
            run_id=run_id,
            pipeline_version=metadata.get("pipeline_version"),
            model_name=metadata.get("model_name"),
            prompt_version=metadata.get("prompt_version"),
            category_version=metadata.get("category_version"),
            audit_count=len(audit_ids),
            audit_ids=audit_ids,
        )
        session.add(pipeline)
        logger.info("Created pipeline_runs row for run_id=%s.", run_id)
    else:
        pipeline.pipeline_version = metadata.get("pipeline_version")
        pipeline.model_name = metadata.get("model_name")
        pipeline.prompt_version = metadata.get("prompt_version")
        pipeline.category_version = metadata.get("category_version")
        pipeline.audit_count = len(audit_ids)
        pipeline.audit_ids = audit_ids
        logger.info("Updated pipeline_runs metadata for run_id=%s.", run_id)

    # Detect duplicate prediction keys before touching the predictions table.
    seen_prediction_keys: set[tuple[int, str]] = set()
    deduplicated_records: list[dict[str, Any]] = []
    duplicate_prediction_keys: set[tuple[int, str]] = set()
    for record in prediction_records:
        key = _pair_key(int(record["audit_id"]), str(record["code"]))
        if key in seen_prediction_keys:
            duplicate_prediction_keys.add(key)
        else:
            seen_prediction_keys.add(key)
            deduplicated_records.append(record)
    if duplicate_prediction_keys:
        rendered = ", ".join(f"({a}, {c})" for a, c in sorted(duplicate_prediction_keys))
        raise ValueError(
            "Duplicate (audit_id, normalized code) pairs found in prediction CSV: "
            f"{rendered}. Refusing to choose an arbitrary prediction row."
        )

    prediction_pairs = {
        _pair_key(int(row["audit_id"]), str(row["code"])) for row in deduplicated_records
    }
    audit_code_map = _get_or_create_audit_codes(session, prediction_pairs)

    existing_predictions: dict[tuple[int, int], OpinionPrediction] = {}
    audit_code_ids = [int(row.audit_code_id) for row in audit_code_map.values()]
    for ids in _chunks(audit_code_ids):
        rows = session.scalars(
            select(OpinionPrediction).where(
                OpinionPrediction.run_id == run_id,
                OpinionPrediction.audit_code_id.in_(ids),
            )
        ).all()
        for row in rows:
            existing_predictions[(int(row.run_id), int(row.audit_code_id))] = row

    inserted = updated = unchanged = 0
    for record in deduplicated_records:
        pair = _pair_key(int(record["audit_id"]), str(record["code"]))
        audit_code = audit_code_map[pair]
        key = (run_id, int(audit_code.audit_code_id))
        current = existing_predictions.get(key)
        if current is None:
            current = OpinionPrediction(
                run_id=run_id,
                audit_code_id=int(audit_code.audit_code_id),
                **{field: record.get(field) for field in PREDICTION_UPDATE_FIELDS},
            )
            session.add(current)
            inserted += 1
        elif _update_fields(current, record, PREDICTION_UPDATE_FIELDS):
            updated += 1
        else:
            unchanged += 1

    session.flush()

    # Identify feedback pairs absent from the current prediction CSV.
    feedback_missing: list[dict[str, Any]] = []
    for audit_id, code_norm in sorted(prediction_pairs):
        feedback_missing.append({"run_id": run_id, "audit_id": audit_id, "code": code_norm})

    logger.info(
        "Prediction import staged: inserted=%d, updated=%d, unchanged=%d, unique_pairs=%d.",
        inserted, updated, unchanged, len(prediction_pairs)
    )
    return {
        "run_id": run_id,
        "audit_code_map": audit_code_map,
        "prediction_pairs": prediction_pairs,
        "prediction_records": deduplicated_records,
        "prediction_counts": {"inserted": inserted, "updated": updated, "unchanged": unchanged},
        "audit_count": len(audit_ids),
    }


def import_feedback_reviews(
    session: Session,
    feedback_records: list[dict[str, Any]],
    prediction_state: dict[str, Any],
    logger,
) -> dict[str, Any]:
    """
    Upsert auditor reviews for pairs present in both prediction and feedback files.

    Duplicate feedback pairs within the same file are considered ambiguous:
    all rows for that pair are skipped. Feedback-only pairs are logged and skipped
    because they have no prediction in this run.
    """
    run_id = int(prediction_state["run_id"])
    audit_code_map: dict[tuple[int, str], AuditCode] = prediction_state["audit_code_map"]
    prediction_pairs: set[tuple[int, str]] = prediction_state["prediction_pairs"]

    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for record in feedback_records:
        grouped[_pair_key(int(record["audit_id"]), str(record["code"]))].append(record)

    duplicate_feedback_pairs = {key for key, rows in grouped.items() if len(rows) > 1}
    feedback_only_pairs = set(grouped) - prediction_pairs
    matching_pairs = (set(grouped) & prediction_pairs) - duplicate_feedback_pairs

    for key in sorted(duplicate_feedback_pairs):
        row_numbers = [row["source_row"] for row in grouped[key]]
        logger.error(
            "Duplicate feedback pair (audit_id=%s, code_norm=%s) at Excel rows %s; all rows skipped.",
            key[0], key[1], row_numbers
        )
    for key in sorted(feedback_only_pairs):
        logger.warning(
            "Feedback pair has no matching prediction in run_id=%s and was skipped: audit_id=%s, code_norm=%s.",
            run_id, key[0], key[1]
        )

    inserted = updated = unchanged = 0
    review_by_pair: dict[tuple[int, str], AuditorReview] = {}
    for pair in sorted(matching_pairs):
        audit_code = audit_code_map[pair]
        feedback = grouped[pair][0]
        review = session.scalar(
            select(AuditorReview).where(
                AuditorReview.run_id == run_id,
                AuditorReview.audit_code_id == audit_code.audit_code_id,
            )
        )
        if review is None:
            review = AuditorReview(
                run_id=run_id,
                audit_code_id=audit_code.audit_code_id,
                **{field: feedback.get(field) for field in REVIEW_UPDATE_FIELDS},
            )
            session.add(review)
            inserted += 1
        elif _update_fields(review, feedback, REVIEW_UPDATE_FIELDS):
            updated += 1
        else:
            unchanged += 1
        review_by_pair[pair] = review

    session.flush()
    logger.info(
        "Feedback import staged: inserted=%d, updated=%d, unchanged=%d, duplicate_pairs_skipped=%d, feedback_only_pairs_skipped=%d.",
        inserted, updated, unchanged, len(duplicate_feedback_pairs), len(feedback_only_pairs)
    )
    return {
        "review_by_pair": review_by_pair,
        "review_counts": {"inserted": inserted, "updated": updated, "unchanged": unchanged},
        "duplicate_feedback_pairs": sorted(duplicate_feedback_pairs),
        "feedback_only_pairs": sorted(feedback_only_pairs),
        "matched_feedback_pairs": sorted(matching_pairs),
    }


def log_missing_feedback(
    prediction_state: dict[str, Any],
    feedback_state: dict[str, Any],
    logger,
    report_dir: str | Path,
) -> Path:
    """
    Report prediction pairs missing from feedback. Duplicate feedback rows are
    still present in the source file, so they count as present for matching
    purposes but are ambiguous and skipped; they are separately logged.
    """
    run_id = int(prediction_state["run_id"])
    prediction_pairs: set[tuple[int, str]] = prediction_state["prediction_pairs"]
    feedback_pairs = set(feedback_state["matched_feedback_pairs"])
    # A pair with duplicate feedback rows is not missing; it is explicitly reported as ambiguous.
    duplicate_pairs = set(feedback_state["duplicate_feedback_pairs"])
    feedback_pairs |= duplicate_pairs
    missing_pairs = sorted(prediction_pairs - feedback_pairs)

    missing_rows = [
        {"run_id": run_id, "audit_id": audit_id, "code": code_norm}
        for audit_id, code_norm in missing_pairs
    ]
    for row in missing_rows:
        logger.warning(
            "Missing auditor feedback for prediction: run_id=%s, audit_id=%s, code_norm=%s.",
            row["run_id"], row["audit_id"], row["code"]
        )

    report_path = _write_missing_feedback_csv(missing_rows, report_dir, run_id)
    logger.info("Missing-feedback report written to %s (%d pair(s)).", report_path, len(missing_rows))
    return report_path
