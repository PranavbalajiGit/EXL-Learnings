import argparse
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def parse_args():
    parser = argparse.ArgumentParser(description="Import CRDT predictions and auditor feedback.")
    parser.add_argument("--predictions", required=True, help="Path to prediction CSV.")
    parser.add_argument("--feedback", required=True, help="Path to standardized feedback .xlsx.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                        help="PostgreSQL URL; defaults to DATABASE_URL in .env.")
    parser.add_argument("--log-dir", default=str(PROJECT_DIR / "logs"))
    parser.add_argument("--report-dir", default=str(PROJECT_DIR / "reports"))
    args = parser.parse_args()

    if not args.database_url:
        parser.error("Provide --database-url or set DATABASE_URL in .env.")
    if not Path(args.predictions).is_file():
        parser.error("Prediction CSV not found: %s" % args.predictions)
    if not Path(args.feedback).is_file():
        parser.error("Feedback Excel file not found: %s" % args.feedback)
    if Path(args.feedback).suffix.lower() not in {".xlsx", ".xlsm"}:
        parser.error("--feedback must point to a .xlsx or .xlsm file.")

    args.predictions = str(Path(args.predictions).resolve())
    args.feedback = str(Path(args.feedback).resolve())
    args.log_dir = str(Path(args.log_dir).resolve())
    args.report_dir = str(Path(args.report_dir).resolve())
    return args
