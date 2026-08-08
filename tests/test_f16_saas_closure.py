from __future__ import annotations

from pathlib import Path

from okto_pulse.community.saas_closure import (
    build_community_saas_closure_report,
)
from okto_pulse.core.application.boundary.saas_closure_report import (
    render_saas_closure_readme,
    validate_saas_closure_readmes,
)
from repo_layout import resolve_core_repo


COMMUNITY_REPO = Path(__file__).resolve().parents[1]

CORE_REPO = resolve_core_repo(COMMUNITY_REPO)


def test_f16_real_core_community_closure_is_terminal() -> None:
    report = build_community_saas_closure_report(
        core_repo=CORE_REPO,
        community_repo=COMMUNITY_REPO,
    )

    assert report.ok, report.findings
    assert len(report.rows) > 4000
    assert all(row.target_owner and row.classification for row in report.rows)
    assert not [row for row in report.rows if row.severity == "blocking"]
    assert all(budget.current == budget.limit == 0 for budget in report.budgets)
    assert report.evidence["distribution_report_ok"] is True
    assert report.evidence["conformance_matrix_ok"] is True
    assert report.evidence["singleton_gate_status"] == "passed"
    assert report.evidence["rebuild_storage_violation_count"] == 0

    expected = render_saas_closure_readme(report)
    assert (
        validate_saas_closure_readmes(
            report,
            core_readme=(CORE_REPO / "README.md").read_text(encoding="utf-8"),
            community_readme=(COMMUNITY_REPO / "README.md").read_text(encoding="utf-8"),
        )
        == ()
    )
    assert expected in (CORE_REPO / "README.md").read_text(encoding="utf-8")
    assert expected in (COMMUNITY_REPO / "README.md").read_text(encoding="utf-8")
