"""
Import CRDT Opinion CSV and auditor feedback Excel files into PostgreSQL.

Expected layout:
project_root/
    opinion_results.csv
    ops_feedback/
        *.xlsx

Install dependencies:
    python -m pip install pandas openpyxl "psycopg[binary]"

Set environment variables before running:
    DATABASE_URL="postgresql://..."
    RUN_ID="123"     # use the actual existing product run ID

Run:
    python import_data.py

Notes:
- Missing optional Excel columns are stored as NULL.
- A workbook is rejected if Audit ID or Code columns are missing.
- Only feedback rows whose normalized (Audit ID, Code) pair exists in the CSV are inserted.
- All matched feedback rows are retained in auditor_reviews.
- Ground truth is created only when exactly one valid Correct/Incorrect review exists
  for a prediction. If there are multiple matched reviews for the same prediction,
  the script reports the ambiguity and does not arbitrarily choose one.
- Source file + Excel row number make feedback imports idempotent on reruns.
"""

import os
import sys
from pathlib import Path
from decimal import Decimal, InvalidOperation

import pandas as pd
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "opinion_results.csv"
FEEDBACK_DIR = ROOT / "ops_feedback"

EXPECTED_FEEDBACK_COLUMNS = {
    "s.no": "source_s_no",
    "audit id": "audit_id",
    "code": "code",
    "auditor comments": "auditor_comments",
    "model outcome results (correct/incorrect)": "model_outcome",
    "ai analysis gap": "ai_analysis_gap",
    "comment": "diagnosis_type",
    "insights": "insights",
    "category": "auditor_category",
}

PREDICTION_COLUMNS = [
    "audit_id", "code", "analysis", "uhdds_analysis",
    "findings", "category", "finding_outcome", "rationale",
]


def normalize_header(value):
    return " ".join(str(value).strip().lower().split())


def normalize_audit_id(value):
    """Return a canonical numeric audit ID string; None for blank/invalid values."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
        if number == number.to_integral_value():
            return str(int(number))
    except (InvalidOperation, ValueError):
        pass
    return text


def normalize_code(value):
    """Trim code whitespace and compare case-insensitively."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text.upper() if text else None


def as_int(value, field_name, source_description):
    if value is None or pd.isna(value) or str(value).strip() == "":
        raise ValueError(f"Missing {field_name} in {source_description}")
    normalized = normalize_audit_id(value)
    try:
        return int(normalized)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid integer {field_name}={value!r} in {source_description}"
        )


def parse_boolean(value, source_description):
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        raise ValueError(f"Missing finding_outcome in {source_description}")
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    raise ValueError(
        f"Invalid finding_outcome={value!r} in {source_description}; expected true/false"
    )


def nullable_text(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def load_predictions():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Could not find {CSV_PATH}")

    df = pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False)
    # Match CSV headers without being sensitive to case/outer whitespace.
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in PREDICTION_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"opinion_results.csv is missing required columns: {missing}")

    rows = []
    keys = set()
    for idx, row in df.iterrows():
        source = f"{CSV_PATH.name}, CSV row {idx + 2}"
        audit_id = as_int(row["audit_id"], "audit_id", source)
        code = normalize_code(row["code"])
        if not code:
            raise ValueError(f"Missing code in {source}")
        outcome = parse_boolean(row["finding_outcome"], source)
        key = (str(audit_id), code)
        if key in keys:
            raise ValueError(
                f"Duplicate (audit_id, code) pair in CSV: {key}. "
                "Resolve duplicates or adjust the uniqueness rule before importing."
            )
        keys.add(key)
        rows.append({
            "audit_id": audit_id,
            "code": code,
            "analysis": nullable_text(row["analysis"]),
            "uhdds_analysis": nullable_text(row["uhdds_analysis"]),
            "findings": nullable_text(row["findings"]),
            "category": nullable_text(row["category"]),
            "finding_outcome": outcome,
            "rationale": nullable_text(row["rationale"]),
            "_key": key,
        })
    return rows, keys


def load_feedback_files(valid_keys):
    if not FEEDBACK_DIR.exists():
        raise FileNotFoundError(f"Could not find feedback folder: {FEEDBACK_DIR}")

    files = sorted(FEEDBACK_DIR.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No .xlsx files found in {FEEDBACK_DIR}")

    matched_rows = []
    rejected_files = []
    unmatched_count = 0

    for path in files:
        try:
            df = pd.read_excel(path, dtype=object, engine="openpyxl")
        except Exception as exc:
            rejected_files.append(f"{path.name}: could not read workbook ({exc})")
            continue

        # Map normalized headers to their original column names.
        original_by_normalized = {}
        for col in df.columns:
            original_by_normalized[normalize_header(col)] = col

        if "audit id" not in original_by_normalized or "code" not in original_by_normalized:
            rejected_files.append(
                f"{path.name}: rejected because required matching columns "
                "'Audit ID' and/or 'Code' are missing"
            )
            continue

        # Optional columns can be missing; their values will be NULL.
        for excel_row_num, (_, source_row) in enumerate(df.iterrows(), start=2):
            audit_value = source_row[original_by_normalized["audit id"]]
            code_value = source_row[original_by_normalized["code"]]
            normalized_audit = normalize_audit_id(audit_value)
            normalized_code = normalize_code(code_value)

            if not normalized_audit or not normalized_code:
                unmatched_count += 1
                continue

            key = (normalized_audit, normalized_code)
            if key not in valid_keys:
                unmatched_count += 1
                continue

            def get_value(normalized_column):
                original = original_by_normalized.get(normalized_column)
                return nullable_text(source_row[original]) if original is not None else None

            s_no_value = get_value("s.no")
            try:
                source_s_no = int(Decimal(s_no_value)) if s_no_value is not None else None
            except (InvalidOperation, ValueError):
                source_s_no = None

            model_outcome = get_value("model outcome results (correct/incorrect)")
            if model_outcome is not None:
                normalized_outcome = model_outcome.strip().lower()
                if normalized_outcome in {"correct", "incorrect"}:
                    model_outcome = normalized_outcome.capitalize()
                else:
                    # Keep the source feedback, but it cannot create ground truth.
                    model_outcome = None

            matched_rows.append({
                "source_file": path.name,
                "source_row": excel_row_num,
                "source_s_no": source_s_no,
                "audit_id": int(normalized_audit),
                "code": normalized_code,
                "auditor_comments": get_value("auditor comments"),
                "model_outcome": model_outcome,
                "ai_analysis_gap": get_value("ai analysis gap"),
                "diagnosis_type": get_value("comment"),
                "insights": get_value("insights"),
                "auditor_category": get_value("category"),
                "_key": key,
            })

    return matched_rows, rejected_files, unmatched_count, len(files)


def main():
    database_url = os.getenv("DATABASE_URL")
    run_id_text = os.getenv("RUN_ID")
    if not database_url:
        raise RuntimeError("Set DATABASE_URL as an environment variable.")
    if not run_id_text:
        raise RuntimeError("Set RUN_ID to the actual existing product run ID.")
    run_id = int(run_id_text)

    prediction_rows, valid_keys = load_predictions()
    feedback_rows, rejected_files, unmatched_count, file_count = load_feedback_files(valid_keys)

    print(f"CSV prediction rows: {len(prediction_rows)}")
    print(f"Excel files discovered: {file_count}")
    print(f"Matched auditor feedback rows: {len(feedback_rows)}")
    print(f"Unmatched/blank-key feedback rows skipped: {unmatched_count}")
    for message in rejected_files:
        print(f"WARNING: {message}")

    # Determine ambiguous predictions before writing.
    review_counts = {}
    for item in feedback_rows:
        review_counts[item["_key"]] = review_counts.get(item["_key"], 0) + 1
    ambiguous_keys = {key for key, count in review_counts.items() if count > 1}
    if ambiguous_keys:
        print(
            f"WARNING: {len(ambiguous_keys)} prediction key(s) have multiple matched "
            "feedback rows. All reviews will be retained, but no ground truth will be "
            "created for those keys until an authoritative-review rule is defined."
        )

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_runs (run_id)
                    VALUES (%s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run_id,),
                )

                # Upsert original predictions by the agreed business key.
                prediction_id_by_key = {}
                for item in prediction_rows:
                    cur.execute(
                        """
                        INSERT INTO opinion_predictions
                            (run_id, audit_id, code, analysis, uhdds_analysis,
                             findings, category, finding_outcome, rationale)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (run_id, audit_id, code)
                        DO UPDATE SET
                            analysis = EXCLUDED.analysis,
                            uhdds_analysis = EXCLUDED.uhdds_analysis,
                            findings = EXCLUDED.findings,
                            category = EXCLUDED.category,
                            finding_outcome = EXCLUDED.finding_outcome,
                            rationale = EXCLUDED.rationale,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING prediction_id
                        """,
                        (
                            run_id, item["audit_id"], item["code"], item["analysis"],
                            item["uhdds_analysis"], item["findings"], item["category"],
                            item["finding_outcome"], item["rationale"],
                        ),
                    )
                    prediction_id_by_key[item["_key"]] = cur.fetchone()["prediction_id"]

                # Import matching feedback. source_file + source_row makes reruns idempotent.
                review_id_by_key = {}
                inserted_or_updated_reviews = 0
                for item in feedback_rows:
                    prediction_id = prediction_id_by_key[item["_key"]]
                    cur.execute(
                        """
                        INSERT INTO auditor_reviews
                            (prediction_id, source_file, source_row, source_s_no,
                             auditor_comments, model_outcome, ai_analysis_gap,
                             diagnosis_type, insights, auditor_category)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (source_file, source_row)
                        DO UPDATE SET
                            prediction_id = EXCLUDED.prediction_id,
                            source_s_no = EXCLUDED.source_s_no,
                            auditor_comments = EXCLUDED.auditor_comments,
                            model_outcome = EXCLUDED.model_outcome,
                            ai_analysis_gap = EXCLUDED.ai_analysis_gap,
                            diagnosis_type = EXCLUDED.diagnosis_type,
                            insights = EXCLUDED.insights,
                            auditor_category = EXCLUDED.auditor_category,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING review_id
                        """,
                        (
                            prediction_id, item["source_file"], item["source_row"],
                            item["source_s_no"], item["auditor_comments"],
                            item["model_outcome"], item["ai_analysis_gap"],
                            item["diagnosis_type"], item["insights"],
                            item["auditor_category"],
                        ),
                    )
                    review_id = cur.fetchone()["review_id"]
                    review_id_by_key.setdefault(item["_key"], []).append(
                        (review_id, item["model_outcome"])
                    )
                    inserted_or_updated_reviews += 1

                # Rebuild ground truth for this run from the currently imported feedback.
                # Keep only one valid review per prediction; never choose arbitrarily.
                cur.execute("DELETE FROM ground_truth WHERE prediction_id IN "
                            "(SELECT prediction_id FROM opinion_predictions WHERE run_id = %s)",
                            (run_id,))

                ground_truth_count = 0
                for item in prediction_rows:
                    key = item["_key"]
                    reviews = review_id_by_key.get(key, [])
                    if len(reviews) != 1:
                        continue
                    review_id, model_outcome = reviews[0]
                    if model_outcome == "Correct":
                        ground_truth_outcome = item["finding_outcome"]
                    elif model_outcome == "Incorrect":
                        ground_truth_outcome = not item["finding_outcome"]
                    else:
                        continue

                    prediction_id = prediction_id_by_key[key]
                    cur.execute(
                        """
                        INSERT INTO ground_truth
                            (prediction_id, review_id, ground_truth_outcome)
                        VALUES (%s, %s, %s)
                        """,
                        (prediction_id, review_id, ground_truth_outcome),
                    )
                    ground_truth_count += 1

                # Update run timestamps.
                cur.execute(
                    "UPDATE pipeline_runs SET updated_at = CURRENT_TIMESTAMP WHERE run_id = %s",
                    (run_id,),
                )

    print("Import completed successfully.")
    print(f"Predictions upserted: {len(prediction_rows)}")
    print(f"Feedback rows inserted/updated: {inserted_or_updated_reviews}")
    print(f"Ground-truth rows created: {ground_truth_count}")
    print("Review any warnings above before using the data for training.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IMPORT FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
