from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditCode, AuditorReview, GroundTruth, OpinionPrediction


def _prediction_outcome_for_pair(
    session: Session,
    run_id: int,
    audit_code_id: int,
) -> bool | None:
    prediction = session.get(
        OpinionPrediction,
        {"run_id": run_id, "audit_code_id": audit_code_id},
    )
    return None if prediction is None else bool(prediction.finding_outcome)


def _upsert_ground_truth(
    session: Session,
    audit_code_id: int,
    finding_outcome: bool,
    source: str,
    review_id: int | None,
) -> tuple[GroundTruth, bool]:
    """
    Upsert the single ground_truth row for an audit_code_id.

    The schema makes audit_code_id unique across all pipeline runs, so this
    updates an existing row in place and preserves ground_truth_id.
    """
    existing = session.scalar(
        select(GroundTruth).where(GroundTruth.audit_code_id == audit_code_id)
    )
    if existing is None:
        row = GroundTruth(
            audit_code_id=audit_code_id,
            finding_outcome=finding_outcome,
            source=source,
            review_id=review_id,
        )
        session.add(row)
        return row, "inserted"

    changed = (
        existing.finding_outcome != finding_outcome
        or existing.source != source
        or existing.review_id != review_id
    )
    if changed:
        existing.finding_outcome = finding_outcome
        existing.source = source
        existing.review_id = review_id
        return existing, "updated"
    return existing, "unchanged"


def synchronize_ground_truth_for_feedback(
    session: Session,
    prediction_state: dict[str, Any],
    feedback_state: dict[str, Any],
    logger,
) -> dict[str, int]:
    """
    Recalculate OPS ground truth for each unambiguous feedback pair in this import.

    Correct -> GT equals current prediction outcome.
    Incorrect -> GT is inverse of current prediction outcome.

    Ground truth is one row per audit_code_id globally, so a row is reassigned to
    the current run's review_id when that pair is reviewed in a later run.
    """
    run_id = int(prediction_state["run_id"])
    audit_code_map: dict[tuple[int, str], AuditCode] = prediction_state["audit_code_map"]
    review_by_pair: dict[tuple[int, str], AuditorReview] = feedback_state["review_by_pair"]

    inserted = updated = unchanged = skipped = 0
    for pair in sorted(feedback_state["matched_feedback_pairs"]):
        audit_code = audit_code_map.get(pair)
        review = review_by_pair.get(pair)
        if audit_code is None or review is None:
            logger.warning(
                "Cannot synchronize ground truth because audit code or review is unavailable: "
                "run_id=%s, audit_id=%s, code_norm=%s.",
                run_id, pair[0], pair[1]
            )
            skipped += 1
            continue

        prediction_outcome = _prediction_outcome_for_pair(
            session, run_id, int(audit_code.audit_code_id)
        )
        if prediction_outcome is None:
            logger.warning(
                "Cannot synchronize ground truth because prediction is unavailable: "
                "run_id=%s, audit_id=%s, code_norm=%s.",
                run_id, pair[0], pair[1]
            )
            skipped += 1
            continue

        if review.model_outcome == "Correct":
            gt_outcome = prediction_outcome
        elif review.model_outcome == "Incorrect":
            gt_outcome = not prediction_outcome
        else:
            logger.error(
                "Unexpected model_outcome %r; ground truth skipped for audit_id=%s, code_norm=%s.",
                review.model_outcome, pair[0], pair[1]
            )
            skipped += 1
            continue

        gt, action = _upsert_ground_truth(
            session=session,
            audit_code_id=int(audit_code.audit_code_id),
            finding_outcome=gt_outcome,
            source="OPS",
            review_id=int(review.review_id),
        )
        if action == "inserted":
            inserted += 1
        elif action == "updated":
            updated += 1
        else:
            unchanged += 1

        logger.info(
            "Ground truth synchronized: run_id=%s, audit_id=%s, code_norm=%s, "
            "model_outcome=%s, prediction_outcome=%s, ground_truth_outcome=%s, source=OPS.",
            run_id, pair[0], pair[1], review.model_outcome, prediction_outcome, gt_outcome
        )

    session.flush()
    logger.info(
        "Ground-truth synchronization staged: inserted=%d, updated=%d, unchanged=%d, skipped=%d.",
        inserted, updated, unchanged, skipped
    )
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "skipped": skipped}


def preserve_existing_ground_truth_when_no_feedback(
    session: Session,
    prediction_state: dict[str, Any],
    feedback_state: dict[str, Any],
    logger,
) -> int:
    """
    Explicitly documents the agreed rule: no new, unambiguous feedback means no
    ground-truth change. Predictions may still be upserted, but existing reviews
    and ground truth are preserved for pairs without new feedback.
    """
    # No writes are performed. This function is intentionally explicit so the
    # CLI can log the policy and avoid accidentally recalculating old GT.
    prediction_pairs = set(prediction_state["prediction_pairs"])
    feedback_pairs = set(feedback_state["matched_feedback_pairs"])
    untouched = len(prediction_pairs - feedback_pairs)
    logger.info(
        "Preserving existing auditor reviews and ground truth for %d prediction pair(s) "
        "without new unambiguous feedback.",
        untouched
    )
    return untouched
