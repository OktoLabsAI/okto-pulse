from __future__ import annotations

import re
from pathlib import Path

from okto_pulse.community import acceptance


ROOT = Path(__file__).resolve().parents[1]
TERMS_TS = ROOT / "frontend" / "src" / "constants" / "terms.ts"


def _typescript_string_constant(name: str) -> str:
    text = TERMS_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {re.escape(name)} = ['\"]([^'\"]+)['\"];", text)
    assert match is not None, f"{name} not found in {TERMS_TS}"
    return match.group(1)


def test_backend_acceptance_terms_constants_match_frontend_terms_source() -> None:
    assert acceptance.TERMS_VERSION == _typescript_string_constant("TERMS_VERSION")
    assert acceptance.TERMS_HASH == _typescript_string_constant("TERMS_HASH")
