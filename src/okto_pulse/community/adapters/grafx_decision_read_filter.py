"""Compile the public normative Decision visibility rule for Grafx reads."""

from okto_pulse.core.ports.spec_projection import INACTIVE_NORMATIVE_DECISION_STATUSES


def normative_decision_filter_clause(node_type: str) -> str:
    if node_type != 'Decision':
        return 'true'
    statuses = ', '.join(f"'{value}'" for value in sorted(INACTIVE_NORMATIVE_DECISION_STATUSES))
    return ("($include_superseded = true OR "
            f"NOT (coalesce(n.source_status, '') IN [{statuses}]))")
