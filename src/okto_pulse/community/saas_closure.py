"""Community composition entry point for the F16 SaaS closure audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from okto_pulse.community.adapters.adapter_provenance import (
    audit_community_adapter_provenance,
)
from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)
from okto_pulse.core.application.boundary.saas_closure_report import (
    SaaSClosureReport,
    build_saas_closure_report,
    render_saas_closure_readme,
    validate_saas_closure_readmes,
)
from okto_pulse.core.application.boundary.repository_checkout import (
    resolve_repository_checkout,
)


def build_community_saas_closure_report(
    *,
    core_repo: str | Path,
    community_repo: str | Path,
    core_wheel: str | Path | None = None,
    community_wheel: str | Path | None = None,
) -> SaaSClosureReport:
    """Compose Community evidence into the Core-owned canonical report."""

    community = Path(community_repo).resolve()
    import_report = audit_community_core_import_boundary(community)
    provenance_report = audit_community_adapter_provenance(community)
    return build_saas_closure_report(
        core_repo=core_repo,
        community_repo=community,
        community_import_report=import_report,
        community_provenance_report=provenance_report,
        core_wheel=core_wheel,
        community_wheel=community_wheel,
    )


def _default_community_repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_core_repo(community_repo: Path) -> Path:
    checkout = resolve_repository_checkout(
        "core",
        anchor_repo=community_repo,
    )
    assert checkout is not None
    return checkout.repo_root


def _payload(
    report: SaaSClosureReport,
    documentation_findings: tuple[Any, ...],
) -> dict[str, Any]:
    payload = report.as_dict()
    payload["documentation_findings"] = [
        {
            "code": item.code,
            "surface": item.surface,
            "location": item.location,
            "detail": item.detail,
        }
        for item in documentation_findings
    ]
    payload["ok"] = report.ok and not documentation_findings
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="okto-pulse-saas-closure")
    parser.add_argument("--core-repo", type=Path)
    parser.add_argument("--community-repo", type=Path)
    parser.add_argument("--core-wheel", type=Path)
    parser.add_argument("--community-wheel", type=Path)
    parser.add_argument("--format", choices=("json", "text", "readme"), default="text")
    parser.add_argument(
        "--no-verify-readmes",
        action="store_true",
        help="Skip exact README projection verification.",
    )
    args = parser.parse_args(argv)

    community_repo = (args.community_repo or _default_community_repo()).resolve()
    core_repo = (args.core_repo or _default_core_repo(community_repo)).resolve()
    report = build_community_saas_closure_report(
        core_repo=core_repo,
        community_repo=community_repo,
        core_wheel=args.core_wheel,
        community_wheel=args.community_wheel,
    )
    documentation_findings = ()
    if not args.no_verify_readmes:
        documentation_findings = validate_saas_closure_readmes(
            report,
            core_readme=(core_repo / "README.md").read_text(encoding="utf-8"),
            community_readme=(community_repo / "README.md").read_text(encoding="utf-8"),
        )

    payload = _payload(report, documentation_findings)
    if args.format == "json":
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    elif args.format == "readme":
        sys.stdout.write(render_saas_closure_readme(report) + "\n")
    else:
        status = "PASS" if payload["ok"] else "FAIL"
        sys.stdout.write(
            f"[{status}] F16 SaaS closure: {len(report.rows)} ownership rows, "
            f"{len(report.findings)} findings, "
            f"{len(documentation_findings)} documentation findings\n"
        )
        for finding in (*report.findings, *documentation_findings):
            sys.stdout.write(
                f"  {finding.code}: {finding.location}: {finding.detail}\n"
            )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_community_saas_closure_report", "main"]
