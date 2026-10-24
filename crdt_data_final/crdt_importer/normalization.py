from __future__ import annotations

import re


def normalize_code_python(code: str) -> str:
    """Mirror the database code_norm rule: uppercase and remove non-alphanumerics."""
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()
