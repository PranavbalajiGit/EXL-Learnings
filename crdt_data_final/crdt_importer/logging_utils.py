from __future__ import annotations

import logging
from pathlib import Path


def configure_run_logger(run_id: int, log_dir: str | Path) -> tuple[logging.Logger, Path]:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"import_run_{run_id}.log"

    logger = logging.getLogger(f"crdt_importer.run.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Avoid duplicate handlers if invoked more than once in one Python process.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path
