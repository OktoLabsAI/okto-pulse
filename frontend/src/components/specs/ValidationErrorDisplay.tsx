import { AlertTriangle } from 'lucide-react';

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
  issue: string;
  action: string | null;
  resources: ResourceGateBlocker[];
}

const RESOURCE_LABELS: Record<string, string> = {
  architecture: 'Architecture',
  mockup: 'Mockup',
  knowledge_base: 'Knowledge Base',
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

  if (code === 'resource_gate_spec_task_coverage' || resources.length > 0 || /resource/i.test(message)) {
    return {
      gateType: 'Resource Coverage',
      issue: message,
      action:
        'Attach or copy each listed spec resource directly to at least one non-cancelled task, or disable the board Resource Gate setting if this check should not apply.',
      resources: resources as ResourceGateBlocker[],
    };
  }

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

  return { gateType, issue, action, resources: [] };
}

export function ValidationErrorDisplay({ error }: { error: string }) {
  const parsed = parseValidationErrorMessage(error);

  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        The following gate must be satisfied before the spec can be validated:
      </p>

      <div className="flex items-center gap-2 mb-2">
        <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 font-semibold uppercase tracking-wide">
          {parsed.gateType}
        </span>
      </div>

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

      {parsed.action && (
        <div className="bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-800 rounded-lg p-4">
          <p className="text-[10px] font-semibold text-amber-700 dark:text-amber-300 uppercase tracking-wide mb-1 flex items-center gap-1.5">
            <AlertTriangle size={12} />
            Required Action
          </p>
          <p className="text-sm text-amber-800 dark:text-amber-200 leading-relaxed">
            {parsed.action}
          </p>
        </div>
      )}
    </div>
  );
}
