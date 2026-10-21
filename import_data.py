"""Import opinion_results.csv and ops_feedback/*.xlsx. Compatible with Python 3.8.

Install:
    python -m pip install pandas openpyxl psycopg2-binary

Set DATABASE_URL and RUN_ID in your Linux terminal, then run:
    python import_data.py
"""

import os
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent
CSV_FILE = ROOT / "opinion_results.csv"
FEEDBACK_FOLDER = ROOT / "ops_feedback"


def text_value(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    return value or None


def audit_id_value(value):
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def code_value(value):
    value = text_value(value)
    return value.upper() if value else None


def read_predictions():
    df = pd.read_csv(CSV_FILE)
    df.columns = [str(c).strip().lower() for c in df.columns]

    required = [
        "audit_id", "code", "analysis", "uhdds_analysis",
        "findings", "category", "finding_outcome", "rationale"
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError("CSV missing columns: " + ", ".join(missing))

    predictions = []
    for _, row in df.iterrows():
        audit_id = audit_id_value(row["audit_id"])
        code = code_value(row["code"])
        if audit_id is None or code is None:
            continue

        value = str(row["finding_outcome"]).strip().lower()
        if value in ("true", "1", "yes"):
            outcome = True
        elif value in ("false", "0", "no"):
            outcome = False
        else:
            raise ValueError("Invalid finding_outcome: " + str(row["finding_outcome"]))

        predictions.append({
            "audit_id": audit_id,
            "code": code,
            "analysis": text_value(row["analysis"]),
            "uhdds_analysis": text_value(row["uhdds_analysis"]),
            "findings": text_value(row["findings"]),
            "category": text_value(row["category"]),
            "finding_outcome": outcome,
            "rationale": text_value(row["rationale"]),
        })
    return predictions


def read_feedback(valid_keys):
    feedback = []

    aliases = {
        "audit id": ["audit id", "auditid", "audit id "],
        "code": ["code"],
        "name": ["name", "auditor name", "auditor"],
        "model outcome results correct incorrect": [
            "model outcome results correct incorrect",
            "model outcome",
            "model outcome results",
        ],
        "auditor comments": ["auditor comments", "auditor comment"],
        "ai analysis gap": ["ai analysis gap"],
        "comment": ["comment"],
        "insights": ["insights"],
        "category": ["category"],
    }

    def normalize(value):
        return (
            str(value).strip().lower()
            if pd.notna(value)
            else ""
        )

    def normalize_header(value):
        import re
        return re.sub(
            r"[^a-z0-9]+", " ", normalize(value)
        ).strip()

    for path in sorted(FEEDBACK_FOLDER.glob("*.xlsx")):
        raw = pd.read_excel(
            path, header=None, engine="openpyxl"
        )

        # Find the row containing the actual column headers.
        header_row = None

        for i, row in raw.iterrows():
            headers = {
                normalize_header(value) for value in row
            }
            if "audit id" in headers and "code" in headers:
                header_row = i
                break

        if header_row is None:
            print("Skipping %s: header row not found" % path.name)
            continue

        df = pd.read_excel(
            path, header=header_row, engine="openpyxl"
        )

        df.columns = [
            normalize_header(column) for column in df.columns
        ]

        # Map alternative headers to standard names.
        for standard_name, possible_names in aliases.items():
            for possible_name in possible_names:
                normalized_alias = normalize_header(possible_name)
                if normalized_alias in df.columns:
                    df.rename(
                        columns={normalized_alias: standard_name},
                        inplace=True,
                    )
                    break

        print("%s: %s" % (path.name, df.columns.tolist()))

        required = [
            "audit id",
            "code",
            "model outcome results correct incorrect",
        ]

        if any(column not in df.columns for column in required):
            print("Skipping %s: missing required columns" % path.name)
            continue

        for _, row in df.iterrows():
            audit_id = audit_id_value(row["audit id"])
            code = code_value(row["code"])

            if (
                audit_id is None
                or code is None
                or (audit_id, code) not in valid_keys
            ):
                continue

            outcome = text_value(
                row["model outcome results correct incorrect"]
            )
            outcome = outcome.capitalize() if outcome else None

            if outcome not in ("Correct", "Incorrect"):
                print(
                    "Skipping row with invalid Model Outcome in %s"
                    % path.name
                )
                continue

            feedback.append({
                "key": (audit_id, code),
                "auditor_name": text_value(row.get("name")),
                "model_outcome": outcome,
                "auditor_comments": text_value(
                    row.get("auditor comments")
                ),
                "ai_analysis_gap": text_value(
                    row.get("ai analysis gap")
                ),
                "diagnosis_type": text_value(row.get("comment")),
                "insights": text_value(row.get("insights")),
                "auditor_category": text_value(row.get("category")),
            })

    return feedback


def main():
    database_url = os.environ.get("DATABASE_URL")
    run_id = os.environ.get("RUN_ID")
    if not database_url or not run_id:
        raise ValueError("Set DATABASE_URL and RUN_ID first.")

    run_id = int(run_id)
    predictions = read_predictions()
    valid_keys = set((p["audit_id"], p["code"]) for p in predictions)
    feedback = read_feedback(valid_keys)

    # Do not guess which auditor review is authoritative if a pair appears more than once.
    counts = {}
    for item in feedback:
        counts[item["key"]] = counts.get(item["key"], 0) + 1
    duplicates = [key for key, count in counts.items() if count > 1]
    if duplicates:
        raise ValueError(
            "Multiple feedback rows match the same (Audit ID, Code). "
            "Resolve duplicates before import. Examples: " + str(duplicates[:10])
        )

    # Cache predictions by key so the script does not repeatedly search the list.
    prediction_by_key = {}
    for item in predictions:
        key = (item["audit_id"], item["code"])
        if key in prediction_by_key:
            raise ValueError("Duplicate (Audit ID, Code) in opinion_results.csv: " + str(key))
        prediction_by_key[key] = item

    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pipeline_runs (run_id) VALUES (%s) ON CONFLICT (run_id) DO NOTHING",
            (run_id,)
        )

        # Re-running this script replaces this run's imported rows.
        cur.execute(
            "DELETE FROM ground_truth WHERE prediction_id IN "
            "(SELECT prediction_id FROM opinion_predictions WHERE run_id = %s)",
            (run_id,)
        )
        cur.execute(
            "DELETE FROM auditor_reviews WHERE prediction_id IN "
            "(SELECT prediction_id FROM opinion_predictions WHERE run_id = %s)",
            (run_id,)
        )
        cur.execute("DELETE FROM opinion_predictions WHERE run_id = %s", (run_id,))

        prediction_ids = {}
        for item in predictions:
            cur.execute(
                """INSERT INTO opinion_predictions
                (run_id, audit_id, code, analysis, uhdds_analysis, findings,
                 category, finding_outcome, rationale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING prediction_id""",
                (
                    run_id, item["audit_id"], item["code"], item["analysis"],
                    item["uhdds_analysis"], item["findings"], item["category"],
                    item["finding_outcome"], item["rationale"]
                )
            )
            prediction_ids[(item["audit_id"], item["code"])] = cur.fetchone()[0]

        ground_truth_count = 0
        for item in feedback:
            key = item["key"]
            prediction_id = prediction_ids[key]
            cur.execute(
                """INSERT INTO auditor_reviews
                (prediction_id, auditor_name, model_outcome, auditor_comments,
                 ai_analysis_gap, diagnosis_type, insights, auditor_category)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING review_id""",
                (
                    prediction_id, item["auditor_name"], item["model_outcome"],
                    item["auditor_comments"], item["ai_analysis_gap"],
                    item["diagnosis_type"], item["insights"], item["auditor_category"]
                )
            )
            review_id = cur.fetchone()[0]

            original = prediction_by_key[key]["finding_outcome"]
            final_outcome = not original if item["model_outcome"] == "Incorrect" else original
            cur.execute(
                """INSERT INTO ground_truth
                (prediction_id, review_id, ground_truth_outcome)
                VALUES (%s, %s, %s)""",
                (prediction_id, review_id, final_outcome)
            )
            ground_truth_count += 1

        cur.execute(
            "UPDATE pipeline_runs SET updated_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (run_id,)
        )
        conn.commit()
        print("Import completed.")
        print("Predictions inserted:", len(predictions))
        print("Matched auditor reviews inserted:", len(feedback))
        print("Ground-truth rows inserted:", ground_truth_count)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
