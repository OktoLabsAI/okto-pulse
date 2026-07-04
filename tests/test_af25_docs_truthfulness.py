from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_docs_match_current_dockerfile_claims() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS base" in dockerfile
    assert "python:3.12-slim" in claude
    assert "python:3.14-slim" not in claude
    assert "HF_MODEL_SHA256" not in claude


def test_readme_documents_implemented_terms_and_metric_guarantees() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Terms acceptance drift is gated" in readme
    assert "Operational metric samples are bounded" in readme
    assert "backend acceptance constants" in readme
