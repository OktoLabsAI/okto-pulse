import { Fragment, useState } from 'react';
import type { Spec } from '@/types';
import type { ReadinessAction, ReadinessIssue, ReadinessResponse } from '@/types/delivery-readiness';
import type { ReadinessNavigator } from './readinessNavigation';
import { DeliveryReadinessIssue } from './DeliveryReadinessIssues';
import { deliveryItemLabel, isRequirementReference } from './deliveryItemLabel';
import { taskDeliveryActionText, taskDeliveryReasons } from './taskDeliveryDiagnosisCopy';

const actionStates: Record<ReadinessAction['state'], string> = { ready: '', waiting_dependency: 'Waiting for another step.',
  requires_input: 'More information is required.', requires_human: 'An authorized human decision is required.',
  forbidden: 'Permission is required. Ask an authorized person to perform this step.', unavailable: 'This step is currently unavailable.' };
const relatedUnit = (unit: ReadinessIssue['affected_units'][number]) => unit.unit === 'scenario' || isRequirementReference(unit.ref);

function stage(issue: ReadinessIssue) {
  if (issue.score_impact === 'verification' && issue.phase === 'implementation') return 'Verification — implementation prerequisite';
  if (issue.score_impact === 'both') return 'Implementation and verification';
  return issue.phase === 'implementation' ? 'Implementation' : issue.phase === 'verification' ? 'Verification' : 'Task completion';
}

function NextStep({ issue, actions, spec, onNavigate }: {
  issue: ReadinessIssue; actions: ReadinessAction[]; spec?: Spec | null; onNavigate: ReadinessNavigator;
}) {
  const matching = actions.filter(action => issue.action_ids.includes(action.id) && action.issue_ids.includes(issue.id));
  const cards = [...new Set(matching.flatMap(action => action.responsible_card_ids))];
  return <div className="space-y-2">
    {matching.length ? matching.map(action => <div key={action.id} className="space-y-1">
      {actionStates[action.state] && <p className="font-medium">{actionStates[action.state]}</p>}
      <p>{taskDeliveryActionText(action.kind, issue.code)}</p>
    </div>) : <p>{taskDeliveryReasons[issue.code]?.next || 'Open the technical evidence to review this check with the responsible person.'}</p>}
    {issue.depends_on_issue_ids.length > 0 && !matching.some(action => action.state === 'waiting_dependency')
      && <p className="text-xs text-gray-500 dark:text-gray-400">Waiting for {issue.depends_on_issue_ids.length} related {issue.depends_on_issue_ids.length === 1 ? 'check' : 'checks'}.</p>}
    {cards.map(id => {
      const card = spec?.cards?.find(candidate => candidate.id === id);
      return card && <p key={id} className="text-xs">Responsible task:{' '}
        {onNavigate.supports?.({ type: 'card', id }) ? <button type="button" className="text-left text-sky-700 underline dark:text-sky-300"
          onClick={() => onNavigate.open({ type: 'card', id })}>{card.title}</button> : card.title}
      </p>;
    })}
  </div>;
}

export function TaskDeliveryIssuesTable({ value, spec, onNavigate, onSnapshotInvalid }: {
  value: ReadinessResponse; spec?: Spec | null; onNavigate: ReadinessNavigator; onSnapshotInvalid: (message: string) => void;
}) {
  const [expanded, setExpanded] = useState<string[]>([]);
  const labelSpec = spec?.id === value.scope.spec_id && spec?.version === value.scope.spec_version
    && spec?.edition === value.scope.spec_edition ? spec : null;
  const groups = [
    { title: 'Requirements and verification', rows: value.issues.filter(issue => issue.affected_units.some(relatedUnit)) },
    { title: 'Task-wide and unassigned checks', rows: value.issues.filter(issue => !issue.affected_units.some(relatedUnit)) },
  ];
  return <div className="space-y-5">
    {groups.filter(group => group.rows.length).map(group => <section key={group.title} aria-label={group.title} className="space-y-2">
      <h5 className="font-medium">{group.title}</h5>
      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table aria-label={group.title} className="w-full table-fixed text-left text-sm min-w-[700px]">
          <thead className="bg-gray-50 text-xs text-gray-600 dark:bg-gray-800/60 dark:text-gray-300"><tr>
            <th scope="col" className="w-[29%] px-3 py-3">Requirement / Objective</th>
            <th scope="col" className="w-[16%] px-3 py-3">Affected stage</th>
            <th scope="col" className="w-[28%] px-3 py-3">What needs attention</th>
            <th scope="col" className="w-[27%] px-3 py-3">Next step</th>
          </tr></thead>
          <tbody>{group.rows.map(issue => {
            const impacted = issue.affected_units.filter(relatedUnit);
            const related = [...new Set(issue.expected.flatMap(fact => fact.obligation_ref && isRequirementReference(fact.obligation_ref)
              && !impacted.some(unit => unit.ref === fact.obligation_ref) ? [fact.obligation_ref] : []))];
            const scenarios = [...new Set([...issue.scenario_ids, ...issue.expected.flatMap(fact => fact.scenario_id ? [fact.scenario_id] : [])])]
              .filter(id => !impacted.some(unit => unit.unit === 'scenario' && unit.ref === id));
            const taskWide = !issue.affected_units_truncated && issue.affected_unit_count === issue.affected_units.length
              && (issue.affected_units.length > 0 || issue.phase === 'lifecycle')
              && issue.affected_units.every(unit => /^(card|spec):/.test(unit.ref));
            return <Fragment key={issue.id}>
              <tr data-readiness-focus={`cause:${issue.id}`} className="border-t border-gray-200 align-top dark:border-gray-700">
                <th scope="row" className="break-words px-3 pb-2 pt-3 font-normal">
                  <div className="space-y-3">{impacted.map(unit => {
                    const label = deliveryItemLabel({ kind: unit.unit, ref: unit.ref }, labelSpec);
                    return <div key={`${unit.unit}:${unit.ref}:${unit.semantic_sha256}`}>
                      <p className="mb-1 text-xs text-gray-500 dark:text-gray-400">{label.family}</p><p>{label.title}</p>
                    </div>;
                  })}</div>
                  {!impacted.length && <p>{taskWide ? 'Task-wide check' : 'Affected requirement not identified'}</p>}
                  {impacted.length > 0 && impacted.length < issue.affected_units.length && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">Other affected items could not be matched to a requirement.</p>}
                  {issue.affected_units_truncated && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                    Showing {issue.affected_units.length} of {issue.affected_unit_count} impacted items. Open technical evidence for the remaining items.
                  </p>}
                </th>
                <td className="break-words px-3 pb-2 pt-3">{stage(issue)}</td>
                <td className="space-y-2 break-words px-3 pb-2 pt-3">
                  <p>{taskDeliveryReasons[issue.code]?.reason || 'An additional delivery check needs attention.'}</p>
                  {related.length > 0 && <div className="space-y-1"><p className="text-xs font-medium">Related requirements:</p>
                    <ul className="list-disc space-y-1 pl-4">{related.map(ref => <li key={ref}>{deliveryItemLabel({ kind: 'obligation', ref }, labelSpec).title}</li>)}</ul>
                  </div>}
                  {scenarios.length > 0 && <div className="space-y-1"><p className="text-xs font-medium">Verification checks:</p>
                    <ul className="list-disc space-y-1 pl-4">{scenarios.map(ref => <li key={ref}>{deliveryItemLabel({ kind: 'scenario', ref }, labelSpec).title}</li>)}</ul>
                  </div>}
                  {issue.scenario_ids_truncated && <p className="text-xs text-amber-700 dark:text-amber-300">More verification checks are listed in the technical evidence.</p>}
                  {issue.expected_truncated && <p className="text-xs text-amber-700 dark:text-amber-300">Additional required information is available in the technical evidence.</p>}
                </td>
                <td className="break-words px-3 pb-2 pt-3"><NextStep issue={issue} actions={value.next_actions} spec={labelSpec} onNavigate={onNavigate} /></td>
              </tr>
              <tr><td colSpan={4} className="px-3 pb-3">
                <DeliveryReadinessIssue issue={issue} onNavigate={onNavigate} onSnapshotInvalid={onSnapshotInvalid}
                  summaryLabel="Evidence and technical details" expanded={expanded.includes(issue.id)}
                  onToggle={open => setExpanded(ids => open ? [...ids, issue.id] : ids.filter(id => id !== issue.id))} />
              </td></tr>
            </Fragment>;
          })}</tbody>
        </table>
      </div>
    </section>)}
    {value.page.total === 0 && <p>{value.scope_complete && value.evaluation_complete
      ? 'No pending checks were identified in this assessment. The delivery status above still determines whether the task is complete.'
      : 'Some checks could not be assessed. No conclusion about pending work is available yet.'}</p>}
    {(value.page.has_more || value.page.response_truncated) && <p className="text-xs text-gray-500 dark:text-gray-400">
      Showing {value.issues.length} of {value.page.total} checks. More checks are available on the next page.
    </p>}
  </div>;
}
