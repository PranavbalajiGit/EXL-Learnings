from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .config import parse_args
from .database import create_database_engine, create_session_factory
from .file_readers import read_feedback_excel, read_predictions_csv
from .import_services import (
    import_feedback_reviews,
    import_predictions_and_codes,
    log_missing_feedback,
)
from .ground_truth_service import (
    preserve_existing_ground_truth_when_no_feedback,
    synchronize_ground_truth_for_feedback,
)
from .logging_utils import configure_run_logger


def _close_run_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def main() -> int:
    args = parse_args()

    # Read and validate input before opening a DB connection.
    prediction_records, metadata = read_predictions_csv(args.predictions)
    feedback_records = read_feedback_excel(args.feedback)
    run_id = int(metadata["run_id"])

    logger, log_path = configure_run_logger(run_id, args.log_dir)
    logger.info("Starting CRDT import for run_id=%s.", run_id)
    logger.info("Prediction CSV: %s", args.predictions)
    logger.info("Feedback Excel: %s", args.feedback)
    logger.info("Log file: %s", log_path)

    engine = None
    session = None
    try:
        engine = create_database_engine(args.database_url)
        session_factory = create_session_factory(engine)
        session = session_factory()

        prediction_state = import_predictions_and_codes(
            session=session,
            prediction_records=prediction_records,
            metadata=metadata,
            logger=logger,
            report_dir=args.report_dir,
        )
        feedback_state = import_feedback_reviews(
            session=session,
            feedback_records=feedback_records,
            prediction_state=prediction_state,
            logger=logger,
        )
        report_path = log_missing_feedback(
            prediction_state=prediction_state,
            feedback_state=feedback_state,
            logger=logger,
            report_dir=args.report_dir,
        )
        ground_truth_counts = synchronize_ground_truth_for_feedback(
            session=session,
            prediction_state=prediction_state,
            feedback_state=feedback_state,
            logger=logger,
        )
        preserved_pairs = preserve_existing_ground_truth_when_no_feedback(
            session=session,
            prediction_state=prediction_state,
            feedback_state=feedback_state,
            logger=logger,
        )

        # One transaction for the complete import.
        session.commit()
        logger.info("Database transaction committed successfully.")

        prediction_counts = prediction_state["prediction_counts"]
        review_counts = feedback_state["review_counts"]
        logger.info("========== IMPORT SUMMARY ==========")
        logger.info("run_id: %s", run_id)
        logger.info("distinct audits: %s", prediction_state["audit_count"])
        logger.info(
            "predictions: inserted=%s updated=%s unchanged=%s",
            prediction_counts["inserted"],
            prediction_counts["updated"],
            prediction_counts["unchanged"],
        )
        logger.info(
            "reviews: inserted=%s updated=%s unchanged=%s",
            review_counts["inserted"],
            review_counts["updated"],
            review_counts["unchanged"],
        )
        logger.info(
            "ground truth: inserted=%s updated=%s unchanged=%s skipped=%s",
            ground_truth_counts["inserted"],
            ground_truth_counts["updated"],
            ground_truth_counts["unchanged"],
            ground_truth_counts["skipped"],
        )
        logger.info("ambiguous duplicate feedback pairs: %s", len(feedback_state["duplicate_feedback_pairs"]))
        logger.info("feedback-only pairs skipped: %s", len(feedback_state["feedback_only_pairs"]))
        logger.info("prediction pairs without unambiguous feedback preserved: %s", preserved_pairs)
        logger.info("missing-feedback report: %s", report_path)
        logger.info("log file: %s", log_path)
        logger.info("Import completed successfully.")
        return 0

    except Exception:
        if session is not None:
            session.rollback()
        logger.exception("Import failed; transaction rolled back.")
        return 1
    finally:
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
        _close_run_logger(logger)


if __name__ == "__main__":
    sys.exit(main())
