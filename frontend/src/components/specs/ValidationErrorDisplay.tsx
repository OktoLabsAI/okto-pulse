import { AlertTriangle, ShieldAlert, Wrench } from 'lucide-react';

interface ResourceGateBlocker {
  resource_type?: string;
  resource_id?: string;
  resource_title?: string | null;
  source_entity_type?: string | null;
  source_entity_title?: string | null;
  reason?: string;
  remediation?: string;
}

interface ParsedValidationError {
  gateType: string;
  gateTypeCode: string | null;
  issue: string;
  action: string | null;
  resources: ResourceGateBlocker[];
  requiredTool: string | null;
  followUpTool: string | null;
  blockedTransition: string | null;
  requiredStatus: string | null;
  enforcementMode: string | null;
  enforcementActive: boolean | null;
  wouldBlockDone: boolean | null;
  // True when the backend supplied a structured gate contract (R4): the UI then
  // renders the REAL gate instead of inferring one from the message text.
  structured: boolean;
}

const RESOURCE_LABELS: Record<string, string> = {
  architecture: 'Architecture',
  mockup: 'Mockup',
  knowledge_base: 'Knowledge Base',
};

// R4-IMP2: map the structured gate_type to a human label so the UI never shows a
// fixed "spec must be validated" copy for a qualitative-evaluation / resource /
// cognitive-readiness / test-card block.
const GATE_TYPE_LABELS: Record<string, string> = {
  spec_validation: 'Spec Validation',
  spec_qualitative_evaluation: 'Qualitative Evaluation',
  test_card_completion: 'Test Card Completion',
  resource_gate: 'Resource Coverage',
  cognitive_readiness: 'Cognitive Readiness',
  state_transition: 'State Transition',
};

function asObject(value: unknown): Record<string, any> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, any>)
    : null;
}

function tryParseJson(raw: string): unknown | null {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function resourceLabel(resourceType?: string): string {
  if (!resourceType) return 'Resource';
  return RESOURCE_LABELS[resourceType] ?? resourceType.replace(/_/g, ' ');
}

function reasonLabel(reason?: string): string {
  if (reason === 'covered_only_by_cancelled_task') {
    return 'Covered only by cancelled task';
  }
  if (reason === 'uncovered') {
    return 'Uncovered';
  }
  return reason ? reason.replace(/_/g, ' ') : 'Blocked';
}

function gateTypeLabel(code: string): string {
  return GATE_TYPE_LABELS[code] ?? code.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function _str(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function _bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

const _EMPTY: Omit<ParsedValidationError, 'gateType' | 'gateTypeCode' | 'issue' | 'action' | 'resources' | 'structured'> = {
  requiredTool: null,
  followUpTool: null,
  blockedTransition: null,
  requiredStatus: null,
  enforcementMode: null,
  enforcementActive: null,
  wouldBlockDone: null,
};

export function parseValidationErrorMessage(error: string): ParsedValidationError {
  const parsed = tryParseJson(error);
  const obj = asObject(parsed);
  const detail = asObject(obj?.detail) ?? obj;
  const code = typeof detail?.error === 'string' ? detail.error : null;
  const message = typeof detail?.message === 'string' ? detail.message : error;
  const details = asObject(detail?.details);
  const resources =
    (Array.isArray(details?.uncovered_resources) ? details?.uncovered_resources : null) ??
    (Array.isArray(detail?.uncovered_resources) ? detail?.uncovered_resources : null) ??
    [];

  // R4-IMP2: prefer the STRUCTURED gate contract (gate_contracts.GateContractError
  // envelope) when present — render the real gate, its required tool, operator
  // action, blocked transition, enforcement mode and would_block_done. Read each
  // field from `details` first, falling back to `detail` so we are robust whether
  // the envelope is wrapped under `details` or flattened on the error object.
  const fromDetails = (key: string): unknown => details?.[key] ?? detail?.[key];
  const gateTypeCode = _str(fromDetails('gate_type'));
  if (gateTypeCode) {
    const reqIdx = message.indexOf('REQUIRED ACTION:');
    const legacyAction = reqIdx > 0 ? message.slice(reqIdx + 16).trim() : null;
    // operator_action preferred; required_action accepted as alias; legacy last.
    const structuredAction =
      _str(fromDetails('operator_action')) ?? _str(fromDetails('required_action'));
    const enforcementActive = _bool(fromDetails('enforcement_active'));
    let enforcementMode = _str(fromDetails('enforcement_mode'));
    if (!enforcementMode && enforcementActive !== null) {
      // Derive a label from the boolean so the badge shows even without a mode.
      enforcementMode = enforcementActive ? 'enforced' : 'advisory';
    }
    return {
      gateType: gateTypeLabel(gateTypeCode),
      gateTypeCode,
      issue: message,
      action: structuredAction ?? legacyAction,
      resources: resources as ResourceGateBlocker[],
      requiredTool: _str(fromDetails('required_tool')),
      followUpTool: _str(fromDetails('follow_up_tool')),
      blockedTransition: _str(fromDetails('blocked_transition')),
      requiredStatus: _str(fromDetails('required_status')),
      enforcementMode,
      enforcementActive,
      wouldBlockDone: _bool(fromDetails('would_block_done')),
      structured: true,
    };
  }

  // Resource Gate (ResourceGateError) — has uncovered_resources but no gate_type.
  if (code === 'resource_gate_spec_task_coverage' || resources.length > 0 || /resource/i.test(message)) {
    return {
      gateType: 'Resource Coverage',
      gateTypeCode: 'resource_gate',
      issue: message,
      action:
        'Attach or copy each listed spec resource directly to at least one non-cancelled task, or disable the board Resource Gate setting if this check should not apply.',
      resources: resources as ResourceGateBlocker[],
      ...(_EMPTY),
      structured: false,
    };
  }

  // Legacy free-text fallback — infer the gate from the message copy.
  const reqIdx = message.indexOf('REQUIRED ACTION:');
  const issue = reqIdx > 0 ? message.slice(0, reqIdx).trim() : message;
  const action = reqIdx > 0 ? message.slice(reqIdx + 16).trim() : null;

  let gateType = 'unknown';
  if (message.includes('test scenario')) { gateType = 'Test Coverage'; }
  else if (message.includes('business rule')) { gateType = 'Rules Coverage'; }
  else if (message.includes('technical requirement') || message.includes('TR')) { gateType = 'TRs Coverage'; }
  else if (message.includes('api contract') || message.includes('contract')) { gateType = 'Contract Coverage'; }
  else if (message.includes('evaluation') || message.includes('approval')) { gateType = 'Qualitative Validation'; }
  else if (message.includes('state machine') || message.includes('transition')) { gateType = 'State Transition'; }

  return {
    gateType,
    gateTypeCode: null,
    issue,
    action,
    resources: [],
    ...(_EMPTY),
    structured: false,
  };
}

export function ValidationErrorDisplay({ error }: { error: string }) {
  const parsed = parseValidationErrorMessage(error);

  // R4-IMP2: header reflects the REAL gate — no fixed "before the spec can be
  // validated" copy when the block is evaluation / resource / cognitive / test-card.
  const headerText =
    parsed.gateTypeCode === 'spec_validation'
      ? 'This spec validation gate must be satisfied before the spec can be validated:'
      : 'The following gate must be satisfied before this action can proceed:';

  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-500 dark:text-gray-400">{headerText}</p>

      <div className="flex flex-wrap items-center gap-2 mb-2">
        <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 font-semibold uppercase tracking-wide">
          {parsed.gateType}
        </span>
        {parsed.enforcementMode && (
          <span
            className="text-[10px] px-2 py-0.5 rounded-full bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200 font-semibold uppercase tracking-wide inline-flex items-center gap-1"
            title={parsed.enforcementActive === false ? 'Advisory: surfaced but not enforced by board policy' : 'Enforced by board policy'}
          >
            <ShieldAlert size={11} />
            {parsed.enforcementMode}
          </span>
        )}
        {parsed.wouldBlockDone && (
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-200 font-semibold uppercase tracking-wide">
            Blocks done
          </span>
        )}
      </div>

      {(parsed.blockedTransition || parsed.requiredStatus) && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {parsed.blockedTransition && (
            <>Blocked transition: <code className="text-gray-700 dark:text-gray-300">{parsed.blockedTransition}</code></>
          )}
          {parsed.blockedTransition && parsed.requiredStatus ? ' · ' : ''}
          {parsed.requiredStatus && (
            <>Required status: <code className="text-gray-700 dark:text-gray-300">{parsed.requiredStatus}</code></>
          )}
        </p>
      )}

      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
        <p className="text-sm text-red-800 dark:text-red-200 leading-relaxed">
          {parsed.issue}
        </p>
      </div>

      {parsed.resources.length > 0 && (
        <div className="bg-gray-50 dark:bg-gray-900/40 border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-2">
          <p className="text-[10px] font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wide">
            Uncovered resources
          </p>
          <div className="space-y-2">
            {parsed.resources.map((resource, index) => (
              <div
                key={`${resource.resource_type}-${resource.resource_id ?? index}`}
                className="rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-2"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-gray-900 dark:text-gray-100 truncate">
                      {resourceLabel(resource.resource_type)}
                      {resource.resource_title ? `: ${resource.resource_title}` : ''}
                    </p>
                    {(resource.source_entity_title || resource.source_entity_type) && (
                      <p className="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                        From {resource.source_entity_title || resource.source_entity_type}
                      </p>
                    )}
                  </div>
                  <span className="shrink-0 text-[10px] px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-200">
                    {reasonLabel(resource.reason)}
                  </span>
                </div>
                {resource.remediation && (
                  <p className="mt-1 text-[11px] text-gray-600 dark:text-gray-300 leading-relaxed">
                    {resource.remediation}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {(parsed.action || parsed.requiredTool) && (
        <div className="bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-800 rounded-lg p-4">
          <p className="text-[10px] font-semibold text-amber-700 dark:text-amber-300 uppercase tracking-wide mb-1 flex items-center gap-1.5">
            <AlertTriangle size={12} />
            Required Action
          </p>
          {parsed.action && (
            <p className="text-sm text-amber-800 dark:text-amber-200 leading-relaxed">
              {parsed.action}
            </p>
          )}
          {parsed.requiredTool && (
            <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-300 inline-flex items-center gap-1.5">
              <Wrench size={11} />
              Use <code className="font-mono">{parsed.requiredTool}</code>
              {parsed.followUpTool && (
                <>, then <code className="font-mono">{parsed.followUpTool}</code></>
              )}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
