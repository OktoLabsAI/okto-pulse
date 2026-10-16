import type { ReadinessResponse } from '@/types/delivery-readiness';

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => [key, stable(item)]));
  return value;
}

/** Copy only the authorized response, retaining limits and human/permission caveats. */
export function readinessDiagnostic(value: ReadinessResponse): string {
  const scope = value.scope;
  const partial = value.page.has_more || value.page.response_truncated;
  const query = { board_id: scope.board_id, spec_id: scope.spec_id, card_id: scope.card_id,
    view: 'readiness', phase: value.page.phase_filter, snapshot_id: value.snapshot_id,
    ...(value.page.next_cursor ? { cursor: value.page.next_cursor } : {}) };
  return [
    `Delivery readiness · ${scope.board_id} / ${scope.spec_id}${scope.card_id ? ' / ' + scope.card_id : ''}`,
    `Edition ${scope.spec_edition} · snapshot ${value.snapshot_id}`,
    ...(['implementation', 'verification'] as const).map(phase => {
      const metric = value[phase];
      return `${phase}: ${metric.percent === null ? metric.status : metric.percent + '%'} · ${metric.satisfied}/${metric.applicable ?? 'unknown'} ${metric.unit}s · ${metric.not_applicable} N/A · ${metric.waived} waived`;
    }),
    `State: ${value.delivery_state}. Stored status: ${value.lifecycle.stored_status}. ${value.summary.message}`,
    `Gate: ${value.delivery_gate.status} (${value.delivery_gate.mode}); exceptions: ${value.delivery_gate.exception_refs.join(', ') || 'none'}.`,
    'Read-only assessment; every mutation must reauthorize and reevaluate.',
    `${partial ? 'PARTIAL summary' : 'Summary'}: ${value.issues.length} of ${value.page.total} causes. Detail queries preserve omitted references and actions.`,
    ...[...value.issues].sort((a, b) => a.id.localeCompare(b.id)).map(issue =>
      `${issue.id} [${issue.code}] ${issue.message}\n` +
      `Affected: ${issue.affected_unit_count}; owner: ${issue.owner ?? issue.owner_state}; dependencies: ${issue.depends_on_issue_ids.join(', ') || 'none'}.\n` +
      `Detail query: ${JSON.stringify(stable(issue.detail_query))}`),
    ...[...value.next_actions].sort((a, b) => a.id.localeCompare(b.id)).map(action =>
      `Action ${action.id} [${action.state}; ${action.execution_surface}]: ${action.message}\n` +
      `Permission: ${action.permission ?? 'read-only/external'}; required inputs: ${action.missing_inputs.join(', ') || 'none'}; dependencies: ${action.depends_on_action_ids.join(', ') || 'none'}.\n` +
      `Tool: ${action.pulse_tool ?? 'none'}; known arguments: ${JSON.stringify(stable(action.arguments))}`),
    `Next/current query: ${JSON.stringify(stable(query))}`,
  ].join('\n\n');
}
