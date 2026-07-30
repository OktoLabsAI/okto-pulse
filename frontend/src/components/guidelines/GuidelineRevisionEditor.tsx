import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Archive,
  History,
  Plus,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react';

import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { ContextualHelpLink } from '@/components/help';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type { Guideline } from '@/types';
import type {
  CreateGuidelineRevisionResponse,
  GuidelineRevisionDetail,
  GuidelineRevisionPatchRequest,
  GuidelineRule,
  GuidelineRuleInput,
  GuidelineLifecycleStatus,
  GuidelineVersionBump,
  PolicyEntityType,
  RetireGuidelineRequest,
} from '@/types/policy-governance';

import {
  POLICY_ENTITY_TYPES,
  POLICY_PREDICATE_CATALOG_VERSION,
  canonicalRuleSet,
  canonicalTags,
  createPolicyClientId,
  factOptionsForTargets,
  guidelineRuleToDraft,
  newRuleDraft,
  operatorNeedsValue,
  operatorsForFact,
  ruleDraftToInput,
  validateRuleDraft,
  validateRuleDrafts,
  type GuidelineRuleDraft,
  type PolicyPredicateDraft,
  type PolicyPredicateOperator,
} from './policyEditorModel';

const REVISION_PAGE_SIZE = 10;

export interface AdoptedGuidelineRevision {
  semanticVersion: string;
  revisionId?: string;
  bindingRevision?: number;
}

export interface GuidelineSuccessorOption {
  guidelineId: string;
  title: string;
  semanticVersion: string;
}

interface GuidelineRevisionEditorProps {
  boardId: string;
  guideline: Guideline;
  adoptedRevision?: AdoptedGuidelineRevision;
  successorOptions?: GuidelineSuccessorOption[];
  onClose: () => void;
  onChanged: () => void | Promise<void>;
}

function authoritativeRuleInput(rule: GuidelineRule): GuidelineRuleInput {
  const [firstPredicate, ...remainingPredicates] = rule.predicates.map(
    (predicate) => ({
      predicate_code: predicate.predicate_code,
      parameters: Object.fromEntries(predicate.parameters),
    }),
  );
  return {
    rule_id: rule.rule_id,
    code: rule.code,
    title: rule.title,
    description: rule.description,
    target_entity_types: [...rule.target_entity_types],
    predicates: [firstPredicate, ...remainingPredicates],
    enforcement: rule.enforcement,
    operator: rule.operator,
    waivable: rule.waivable,
    policy_class: rule.policy_class,
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    const minimum =
      error.details.minimum_semantic_version
      ?? error.details.minimum_bump;
    if (error.kind === 'under_bump' && minimum) {
      return `Declared version is too low. Minimum required: ${minimum}.`;
    }
    if (error.nextAction) return `${error.message} Next: ${error.nextAction}.`;
    return error.message;
  }
  return error instanceof Error ? error.message : 'Unexpected policy error.';
}

function bumpLabel(bump: GuidelineVersionBump): string {
  return `${bump.charAt(0).toUpperCase()}${bump.slice(1)} bump`;
}

function RuleEditorCard({
  rule,
  index,
  disabled,
  onChange,
  onRemove,
}: {
  rule: GuidelineRuleDraft;
  index: number;
  disabled: boolean;
  onChange: (next: GuidelineRuleDraft) => void;
  onRemove: () => void;
}) {
  const factOptions = factOptionsForTargets(rule.targetEntityTypes);
  const validationError = validateRuleDraft(rule);

  const updatePredicate = (
    predicateIndex: number,
    patch: Partial<PolicyPredicateDraft>,
  ) => {
    onChange({
      ...rule,
      predicates: rule.predicates.map((predicate, currentIndex) =>
        currentIndex === predicateIndex
          ? { ...predicate, ...patch }
          : predicate,
      ),
    });
  };

  const toggleTarget = (target: PolicyEntityType) => {
    onChange({
      ...rule,
      targetEntityTypes: rule.targetEntityTypes.includes(target)
        ? rule.targetEntityTypes.filter((item) => item !== target)
        : [...rule.targetEntityTypes, target],
    });
  };

  const addPredicate = () => {
    onChange({
      ...rule,
      predicates: [
        ...rule.predicates,
        {
          localId: createPolicyClientId('predicate-draft'),
          fact: 'status',
          operator: 'exists',
          rawValue: '',
        },
      ],
    });
  };

  return (
    <article
      className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900"
      data-testid={`policy-rule-editor-${index}`}
    >
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-gray-900 dark:text-white">
            Rule {index + 1}
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <span className="rounded bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-500/15 dark:text-blue-200">
              {rule.enforcement}
            </span>
            {rule.enforcement === 'blocking' && (
              <span className="rounded bg-red-50 px-2 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-500/15 dark:text-red-200">
                Can block supported gates
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label={`Remove rule ${index + 1}`}
            disabled={disabled}
            onClick={onRemove}
            className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-500/10"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Rule ID
          <input
            value={rule.ruleId}
            disabled={disabled}
            onChange={(event) => onChange({ ...rule, ruleId: event.target.value })}
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Code
          <input
            value={rule.code}
            disabled={disabled}
            onChange={(event) => onChange({ ...rule, code: event.target.value })}
            placeholder="require_acceptance_criteria"
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Title
          <input
            value={rule.title}
            disabled={disabled}
            onChange={(event) => onChange({ ...rule, title: event.target.value })}
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Policy class
          <input
            value={rule.policyClass}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...rule, policyClass: event.target.value })
            }
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
      </div>

      <label className="mt-3 block text-xs font-medium text-gray-600 dark:text-gray-300">
        Description
        <textarea
          value={rule.description}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...rule, description: event.target.value })
          }
          rows={2}
          className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
        />
      </label>

      <fieldset className="mt-3">
        <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
          Executable targets
        </legend>
        <p className="mb-2 mt-0.5 text-[11px] text-gray-500">
          Targets control evaluation. They do not change the guideline context,
          which remains available to all entities.
        </p>
        <div className="flex flex-wrap gap-2">
          {POLICY_ENTITY_TYPES.map((target) => (
            <label
              key={target}
              className="inline-flex items-center gap-1.5 rounded border border-gray-200 px-2 py-1 text-xs text-gray-700 dark:border-gray-700 dark:text-gray-200"
            >
              <input
                type="checkbox"
                checked={rule.targetEntityTypes.includes(target)}
                disabled={disabled}
                onChange={() => toggleTarget(target)}
              />
              {target}
            </label>
          ))}
        </div>
      </fieldset>

      <div className="mt-3 grid gap-3 md:grid-cols-4">
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Enforcement
          <select
            value={rule.enforcement}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...rule,
                enforcement: event.target.value as 'advisory' | 'blocking',
              })
            }
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-950"
          >
            <option value="advisory">Advisory (default)</option>
            <option value="blocking">Blocking</option>
          </select>
        </label>
        <label className="text-xs font-medium text-gray-600 dark:text-gray-300">
          Predicate logic
          <select
            value={rule.operator}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...rule,
                operator: event.target.value as 'all' | 'any',
              })
            }
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-950"
          >
            <option value="all">All predicates</option>
            <option value="any">Any predicate</option>
          </select>
        </label>
        <label className="mt-5 inline-flex items-center gap-2 text-xs text-gray-700 dark:text-gray-200">
          <input
            type="checkbox"
            checked={rule.waivable}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...rule, waivable: event.target.checked })
            }
          />
          Waivable
        </label>
      </div>

      <div className="mt-4 rounded-md border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-950/50">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs font-semibold text-gray-700 dark:text-gray-200">
              Deterministic predicates
            </div>
            <p className="text-[11px] text-gray-500">
              Only server-owned facts and compatible{' '}
              {POLICY_PREDICATE_CATALOG_VERSION} operators are offered.
            </p>
          </div>
          <button
            type="button"
            disabled={disabled}
            onClick={addPredicate}
            className="inline-flex items-center gap-1 rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-white disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-900"
          >
            <Plus size={12} />
            Predicate
          </button>
        </div>

        <div className="mt-3 space-y-2">
          {rule.predicates.map((predicate, predicateIndex) => {
            const selectedFact =
              factOptions.find((fact) => fact.code === predicate.fact)
              ?? factOptions[0];
            const operators = selectedFact
              ? operatorsForFact(selectedFact)
              : [];
            return (
              <div
                key={predicate.localId}
                className="grid items-end gap-2 md:grid-cols-[minmax(0,1fr)_150px_minmax(0,1fr)_32px]"
              >
                <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
                  Fact
                  <select
                    aria-label={`Rule ${index + 1} predicate ${predicateIndex + 1} fact`}
                    value={predicate.fact}
                    disabled={disabled}
                    onChange={(event) => {
                      const fact = factOptions.find(
                        (item) => item.code === event.target.value,
                      );
                      updatePredicate(predicateIndex, {
                        fact: event.target.value,
                        operator: fact
                          ? operatorsForFact(fact)[0]
                          : 'exists',
                        rawValue: '',
                      });
                    }}
                    className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-900"
                  >
                    {factOptions.map((fact) => (
                      <option key={fact.code} value={fact.code}>
                        {fact.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
                  Operator
                  <select
                    aria-label={`Rule ${index + 1} predicate ${predicateIndex + 1} operator`}
                    value={predicate.operator}
                    disabled={disabled}
                    onChange={(event) => {
                      const operator =
                        event.target.value as PolicyPredicateOperator;
                      updatePredicate(predicateIndex, {
                        operator,
                        rawValue: operatorNeedsValue(operator)
                          ? predicate.rawValue
                          : '',
                      });
                    }}
                    className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-900"
                  >
                    {operators.map((operator) => (
                      <option key={operator} value={operator}>
                        {operator}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
                  Value
                  {selectedFact?.kind === 'boolean'
                    && operatorNeedsValue(predicate.operator) ? (
                       <select
                         aria-label={`Rule ${index + 1} predicate ${predicateIndex + 1} value`}
                         value={predicate.rawValue}
                         disabled={disabled}
                        onChange={(event) =>
                          updatePredicate(predicateIndex, {
                            rawValue: event.target.value,
                          })
                        }
                         className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-900"
                       >
                         <option value="">Select a value</option>
                         <option value="true">true</option>
                         <option value="false">false</option>
                      </select>
                    ) : selectedFact?.allowedValues
                      && selectedFact.allowedValues.length > 0
                      && operatorNeedsValue(predicate.operator)
                      && predicate.operator !== 'in'
                      && predicate.operator !== 'not_in' ? (
                        <select
                          aria-label={`Rule ${index + 1} predicate ${predicateIndex + 1} value`}
                          value={predicate.rawValue}
                          disabled={disabled}
                          onChange={(event) =>
                            updatePredicate(predicateIndex, {
                              rawValue: event.target.value,
                            })
                          }
                          className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-700 dark:bg-gray-900"
                        >
                          <option value="">Select a value</option>
                          {selectedFact.allowedValues.map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                    ) : (
                      <input
                        aria-label={`Rule ${index + 1} predicate ${predicateIndex + 1} value`}
                        value={predicate.rawValue}
                        disabled={
                          disabled || !operatorNeedsValue(predicate.operator)
                        }
                        type={
                          selectedFact?.kind === 'integer'
                          || selectedFact?.kind === 'number'
                          || predicate.operator.startsWith('count_')
                            ? 'number'
                            : 'text'
                        }
                        min={
                          predicate.operator.startsWith('count_')
                            ? 0
                            : selectedFact?.minimum
                        }
                        max={selectedFact?.maximum}
                        step={
                          selectedFact?.kind === 'integer'
                          || predicate.operator.startsWith('count_')
                            ? 1
                            : selectedFact?.kind === 'number'
                              ? 'any'
                              : undefined
                        }
                        onChange={(event) =>
                          updatePredicate(predicateIndex, {
                            rawValue: event.target.value,
                          })
                        }
                        placeholder={
                          predicate.operator === 'in'
                            || predicate.operator === 'not_in'
                            ? 'comma-separated values'
                            : operatorNeedsValue(predicate.operator)
                              ? 'value'
                              : 'not required'
                        }
                        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-2 text-xs disabled:bg-gray-100 dark:border-gray-700 dark:bg-gray-900 dark:disabled:bg-gray-800"
                      />
                    )}
                </label>
                <button
                  type="button"
                  aria-label={`Remove predicate ${predicateIndex + 1}`}
                  disabled={disabled || rule.predicates.length === 1}
                  onClick={() =>
                    onChange({
                      ...rule,
                      predicates: rule.predicates.filter(
                        (_, currentIndex) => currentIndex !== predicateIndex,
                      ),
                    })
                  }
                  className="mb-0.5 rounded p-2 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-500/10"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {validationError && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          {validationError}
        </p>
      )}
    </article>
  );
}

function RetirementDialog({
  guidelineTitle,
  successorOptions,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  guidelineTitle: string;
  successorOptions: GuidelineSuccessorOption[];
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (request: RetireGuidelineRequest) => void;
}) {
  const [status, setStatus] = useState<'retired' | 'superseded'>('retired');
  const [reason, setReason] = useState('');
  const [successor, setSuccessor] = useState('');
  const commandIdentityRef = useRef({
    signature: '',
    retirementId: createPolicyClientId('retirement'),
    idempotencyKey: createPolicyClientId('retirement-command'),
  });
  const focusTrap = useDialogFocusTrap(true, '[data-dialog-initial-focus]');
  useEscapeToClose(onCancel, {
    enabled: true,
    canClose: !busy,
    priority: 30,
  });

  const valid =
    reason.trim().length > 0
    && (status === 'retired' || successor.trim().length > 0);

  const confirm = () => {
    if (!valid || busy) return;
    const signature = JSON.stringify({
      status,
      reason: reason.trim(),
      successor: status === 'superseded' ? successor : null,
    });
    if (commandIdentityRef.current.signature !== signature) {
      commandIdentityRef.current = {
        signature,
        retirementId: createPolicyClientId('retirement'),
        idempotencyKey: createPolicyClientId('retirement-command'),
      };
    }
    const identity = commandIdentityRef.current;
    if (status === 'superseded') {
      onConfirm({
        retirement_id: identity.retirementId,
        idempotency_key: identity.idempotencyKey,
        status: 'superseded',
        reason: reason.trim(),
        superseded_by_guideline_id: successor,
      });
      return;
    }
    onConfirm({
      retirement_id: identity.retirementId,
      idempotency_key: identity.idempotencyKey,
      status: 'retired',
      reason: reason.trim(),
    });
  };

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/55 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="policy-retirement-title"
        aria-describedby="policy-retirement-description"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="w-full max-w-lg rounded-xl bg-white shadow-2xl dark:bg-gray-900"
      >
        <header className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <h3
            id="policy-retirement-title"
            className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-white"
          >
            <Archive size={18} className="text-amber-500" />
            Retire guideline
          </h3>
          <p
            id="policy-retirement-description"
            className="mt-1 text-sm text-gray-500"
          >
            {guidelineTitle} will remain resolvable in immutable history. This
            is not a hard delete.
          </p>
        </header>
        <div className="space-y-4 px-5 py-4">
          <fieldset>
            <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
              Lifecycle outcome
            </legend>
            <div className="mt-2 flex gap-4">
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  data-dialog-initial-focus
                  checked={status === 'retired'}
                  disabled={busy}
                  onChange={() => setStatus('retired')}
                />
                Retired
              </label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  checked={status === 'superseded'}
                  disabled={busy || successorOptions.length === 0}
                  onChange={() => setStatus('superseded')}
                />
                Superseded
              </label>
            </div>
          </fieldset>
          {status === 'superseded' && (
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
              Successor guideline
              <select
                value={successor}
                disabled={busy}
                onChange={(event) => setSuccessor(event.target.value)}
                className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-xs dark:border-gray-700 dark:bg-gray-950"
              >
                <option value="">Select an active global guideline</option>
                {successorOptions.map((option) => (
                  <option key={option.guidelineId} value={option.guidelineId}>
                    {option.title} · v{option.semanticVersion}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
            Reason
            <textarea
              value={reason}
              disabled={busy}
              onChange={(event) => setReason(event.target.value)}
              rows={4}
              placeholder="Why should this guideline stop accepting new adoptions?"
              className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-950"
            />
          </label>
          {error && (
            <div
              role="alert"
              className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
            >
              {error}
            </div>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-gray-200 px-5 py-4 dark:border-gray-700">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="btn btn-secondary"
          >
            Keep active
          </button>
          <button
            type="button"
            disabled={!valid || busy}
            onClick={confirm}
            className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? 'Retiring…' : 'Confirm retirement'}
          </button>
        </footer>
      </div>
    </div>
  );
}

export function GuidelineRevisionEditor({
  boardId,
  guideline,
  adoptedRevision,
  successorOptions = [],
  onClose,
  onChanged,
}: GuidelineRevisionEditorProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const canRead =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
    && permissions.has('guidelines.revisions.read');
  const canCreate =
    canRead && permissions.has('guidelines.revisions.create');
  const canAuthorRules =
    canCreate && permissions.has('guidelines.rules.author_blocking');
  const canRetire =
    canRead && permissions.has('guidelines.revisions.retire');
  const authorityError = permissions.error
    ? 'Permission status is unavailable. Policy mutations fail closed.'
    : permissions.ownerReviewRequired
      ? 'Owner review is required before policy actions are available.'
      : !permissions.isLoading && !canRead
        ? 'You do not have permission to read guideline revisions.'
        : null;
  const focusTrap = useDialogFocusTrap(
    true,
    '[data-dialog-initial-focus]',
  );
  const [revisions, setRevisions] = useState<GuidelineRevisionDetail[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [initialLoadError, setInitialLoadError] = useState<string | null>(null);
  const [paginationError, setPaginationError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationResult, setMutationResult] = useState<string | null>(null);
  const [retirementStatus, setRetirementStatus] =
    useState<GuidelineLifecycleStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [retirementOpen, setRetirementOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [tags, setTags] = useState('');
  const [rules, setRules] = useState<GuidelineRuleDraft[]>([]);
  const [declaredVersion, setDeclaredVersion] = useState('');
  const seenHistoryCursorsRef = useRef(new Set<string>());
  const revisionCommandRef = useRef({
    signature: '',
    idempotencyKey: createPolicyClientId('revision-command'),
  });

  useEscapeToClose(onClose, {
    enabled: !retirementOpen,
    canClose: !saving,
    priority: 20,
  });

  const latest = revisions[0];
  const busy =
    permissions.isLoading || loading || loadingMore || saving;

  const resetDraft = useCallback((revision: GuidelineRevisionDetail) => {
    setTitle(revision.title);
    setContent(revision.content);
    setTags(revision.tags.join(', '));
    setRules(revision.rules.map(guidelineRuleToDraft));
    setDeclaredVersion('');
    setMutationError(null);
  }, []);

  const loadFirstPage = useCallback(async () => {
    if (!canRead) {
      setLoading(false);
      setInitialLoadError(authorityError);
      setRevisions([]);
      setRetirementStatus(null);
      setNextCursor(null);
      return;
    }
    setLoading(true);
    setInitialLoadError(null);
    setPaginationError(null);
    seenHistoryCursorsRef.current.clear();
    try {
      const page = await api.listGuidelineRevisions(
        boardId,
        guideline.id,
        { limit: REVISION_PAGE_SIZE, projection: 'detail' },
      );
      const detailItems = page.items.filter(
        (item): item is GuidelineRevisionDetail =>
          item.projection === 'detail',
      );
      if (detailItems.length === 0) {
        throw new Error('No immutable revisions were returned.');
      }
      const authority = await api.getGuidelineRevision(
        boardId,
        guideline.id,
        detailItems[0].revision_id,
      );
      if (
        authority.head.revision_id !== detailItems[0].revision_id
        || authority.revision.revision_id !== detailItems[0].revision_id
      ) {
        throw new Error('Authoritative guideline head changed while loading.');
      }
      setRevisions(detailItems);
      setRetirementStatus(authority.retirement?.status ?? null);
      setNextCursor(page.has_more ? page.next_cursor : null);
      resetDraft(detailItems[0]);
    } catch (error) {
      setInitialLoadError(errorMessage(error));
      setRevisions([]);
      setRetirementStatus(null);
      setNextCursor(null);
    } finally {
      setLoading(false);
    }
  }, [
    api,
    authorityError,
    boardId,
    canRead,
    guideline.id,
    resetDraft,
  ]);

  useEffect(() => {
    if (permissions.isLoading) return;
    void loadFirstPage();
  }, [loadFirstPage, permissions.isLoading]);

  const draftInputs = useMemo(() => {
    try {
      return rules.map(ruleDraftToInput);
    } catch {
      return null;
    }
  }, [rules]);

  const currentRuleInputs = useMemo(
    () => latest?.rules.map(authoritativeRuleInput) ?? [],
    [latest],
  );

  const parsedTags = useMemo(
    () => canonicalTags(tags.split(',')),
    [tags],
  );
  const tagError = useMemo(() => {
    const rawTags = tags
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean);
    return new Set(rawTags).size === rawTags.length
      ? null
      : 'Tags must be unique within a revision.';
  }, [tags]);

  const changeSummary = useMemo(() => {
    if (!latest) return [];
    const changes: string[] = [];
    if (title.trim() !== latest.title) changes.push('title');
    if (content.trim() !== latest.content) changes.push('content');
    if (
      JSON.stringify(parsedTags)
      !== JSON.stringify(canonicalTags(latest.tags))
    ) {
      changes.push('tags');
    }
    if (
      draftInputs
      && JSON.stringify(canonicalRuleSet(draftInputs))
        !== JSON.stringify(canonicalRuleSet(currentRuleInputs))
    ) {
      changes.push('executable rules');
    }
    return changes;
  }, [content, currentRuleInputs, draftInputs, latest, parsedTags, title]);

  const ruleError = useMemo(() => validateRuleDrafts(rules), [rules]);

  const saveRevision = async () => {
    if (
      !latest
      || !draftInputs
      || ruleError
      || tagError
      || !canCreate
      || !title.trim()
      || !content.trim()
      || changeSummary.length === 0
    ) {
      return;
    }

    const patch: Partial<{
      title: string;
      content: string;
      tags: string[];
      rules: GuidelineRuleInput[];
    }> = {};
    if (title.trim() !== latest.title) patch.title = title.trim();
    if (content.trim() !== latest.content) patch.content = content.trim();
    if (
      JSON.stringify(parsedTags)
      !== JSON.stringify(canonicalTags(latest.tags))
    ) {
      patch.tags = parsedTags;
    }
    if (changeSummary.includes('executable rules')) {
      if (!canAuthorRules) {
        setMutationError(
          'Changing executable rules requires guidelines.rules.author_blocking.',
        );
        return;
      }
      patch.rules = draftInputs;
    }
    const commandSignature = JSON.stringify({
      patch,
      declaredSemanticVersion: declaredVersion.trim() || null,
    });
    if (revisionCommandRef.current.signature !== commandSignature) {
      revisionCommandRef.current = {
        signature: commandSignature,
        idempotencyKey: createPolicyClientId('revision-command'),
      };
    }

    setSaving(true);
    setMutationError(null);
    setMutationResult(null);
    try {
      const response: CreateGuidelineRevisionResponse =
        await api.createGuidelineRevision(
          boardId,
          guideline.id,
          {
            idempotency_key: revisionCommandRef.current.idempotencyKey,
            patch: patch as GuidelineRevisionPatchRequest,
            ...(declaredVersion.trim()
              ? { declared_semantic_version: declaredVersion.trim() }
              : {}),
          },
        );
      if (response.status === 'applied') {
        setMutationResult(
          `Created v${response.revision.semantic_version} · ${bumpLabel(response.minimum_bump)}.`,
        );
        await loadFirstPage();
        await onChanged();
      } else {
        setMutationResult('No revision was created because the request was an idempotent no-op.');
      }
    } catch (error) {
      setMutationError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const loadOlder = async () => {
    if (!nextCursor || loadingMore) return;
    const requestCursor = nextCursor;
    if (seenHistoryCursorsRef.current.has(requestCursor)) {
      setPaginationError(
        'The revision cursor repeated. Restart history before continuing.',
      );
      setNextCursor(null);
      return;
    }
    seenHistoryCursorsRef.current.add(requestCursor);
    setLoadingMore(true);
    setPaginationError(null);
    try {
      const page = await api.listGuidelineRevisions(
        boardId,
        guideline.id,
        {
          limit: REVISION_PAGE_SIZE,
          projection: 'detail',
          cursor: requestCursor,
        },
      );
      const detailItems = page.items.filter(
          (item): item is GuidelineRevisionDetail =>
            item.projection === 'detail',
        );
      setRevisions((current) => {
        const existing = new Set(current.map((item) => item.revision_id));
        return [
          ...current,
          ...detailItems.filter((item) => !existing.has(item.revision_id)),
        ];
      });
      if (
        page.has_more
        && (
          page.next_cursor === requestCursor
          || seenHistoryCursorsRef.current.has(page.next_cursor)
        )
      ) {
        setNextCursor(null);
        setPaginationError(
          'The server returned a repeated cursor. Restart history before continuing.',
        );
      } else {
        setNextCursor(page.has_more ? page.next_cursor : null);
      }
    } catch (error) {
      if (
        error instanceof PolicyGovernanceApiError
        && error.kind === 'invalid_cursor'
      ) {
        setNextCursor(null);
        setPaginationError(
          'This history cursor expired or no longer matches the projection. Restart history.',
        );
      } else {
        setPaginationError(errorMessage(error));
      }
    } finally {
      setLoadingMore(false);
    }
  };

  const retire = async (request: RetireGuidelineRequest) => {
    if (!canRetire) return;
    setSaving(true);
    setMutationError(null);
    try {
      await api.retireGuideline(boardId, guideline.id, request);
      setRetirementOpen(false);
      await onChanged();
      onClose();
    } catch (error) {
      setMutationError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const executableTargets = latest
    ? Array.from(
        new Set(latest.rules.flatMap((rule) => rule.target_entity_types)),
      )
    : [];
  const containsBlockingRules =
    latest?.rules.some((rule) => rule.enforcement === 'blocking') ?? false;
  const revisionUpdateAvailable = Boolean(
    latest
    && adoptedRevision
    && (
      adoptedRevision.revisionId
        ? latest.revision_id !== adoptedRevision.revisionId
        : latest.semantic_version !== adoptedRevision.semanticVersion
    ),
  );

  const updateRule = (index: number, next: GuidelineRuleDraft) => {
    setRules((current) =>
      current.map((rule, currentIndex) =>
        currentIndex === index ? next : rule,
      ),
    );
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/55 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="guideline-revision-editor-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex h-[94vh] w-full max-w-7xl flex-col overflow-hidden rounded-xl border border-gray-200 bg-gray-50 shadow-2xl dark:border-gray-700 dark:bg-gray-950"
      >
        <header className="flex items-start justify-between gap-4 border-b border-gray-200 bg-white px-6 py-4 dark:border-gray-700 dark:bg-gray-900">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-blue-600 dark:text-blue-300">
              Immutable policy guideline
            </div>
            <h2
              id="guideline-revision-editor-title"
              className="mt-1 text-xl font-semibold text-gray-900 dark:text-white"
            >
              {guideline.title}
            </h2>
            <p className="mt-1 text-sm text-gray-500">
              Create append-only revisions. Existing revision content is never
              edited in place.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ContextualHelpLink
              sectionId="policy-governance"
              testId="guideline-revision-help"
            >
              Revision guide
            </ContextualHelpLink>
            <button
              type="button"
              data-dialog-initial-focus
              aria-label="Close revision editor"
              disabled={saving}
              onClick={onClose}
              className="rounded-lg p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-800"
            >
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="grid min-h-0 flex-1 lg:grid-cols-[320px_minmax(0,1fr)]">
          <aside className="overflow-y-auto border-r border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[10px] font-semibold uppercase text-gray-500">
                  Adopted
                </div>
                <div className="mt-1 text-sm font-semibold text-gray-900 dark:text-white">
                  {adoptedRevision
                    ? `v${adoptedRevision.semanticVersion}`
                    : 'Not adopted'}
                </div>
                {adoptedRevision?.bindingRevision !== undefined && (
                  <div className="mt-0.5 text-[10px] text-gray-500">
                    Binding revision {adoptedRevision.bindingRevision}
                  </div>
                )}
              </div>
              <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[10px] font-semibold uppercase text-gray-500">
                  Latest
                </div>
                <div className="mt-1 text-sm font-semibold text-gray-900 dark:text-white">
                  {latest ? `v${latest.semantic_version}` : 'Unavailable'}
                </div>
                {revisionUpdateAvailable && (
                    <div className="mt-0.5 text-[10px] text-amber-600 dark:text-amber-300">
                      Update available
                    </div>
                  )}
              </div>
            </div>

            <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-500/30 dark:bg-blue-500/10">
              <div className="text-[10px] font-semibold uppercase text-blue-700 dark:text-blue-200">
                Context scope
              </div>
              <div className="mt-1 text-sm font-semibold text-blue-950 dark:text-blue-100">
                All entities
              </div>
              <p className="mt-1 text-xs text-blue-800/75 dark:text-blue-100/70">
                Context availability is independent from executable targets
                and enforcement.
              </p>
            </div>

            <div className="mt-4">
              <div className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                Executable targets
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {executableTargets.length > 0 ? (
                  executableTargets.map((target) => (
                    <span
                      key={target}
                      className="rounded bg-gray-100 px-2 py-1 text-[10px] text-gray-700 dark:bg-gray-800 dark:text-gray-200"
                    >
                      {target}
                    </span>
                  ))
                ) : (
                  <span className="text-xs text-gray-500">
                    Context only · no executable rules
                  </span>
                )}
              </div>
              {containsBlockingRules && (
                <div className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-red-50 px-2.5 py-1 text-xs font-medium text-red-700 dark:bg-red-500/15 dark:text-red-200">
                  <ShieldAlert size={13} />
                  Contains blocking rules
                </div>
              )}
            </div>

            <div className="mt-5 border-t border-gray-200 pt-4 dark:border-gray-700">
              <div className="flex items-center gap-2 text-xs font-semibold text-gray-700 dark:text-gray-200">
                <History size={14} />
                Revision history
              </div>
              <div className="mt-2 space-y-2" data-testid="guideline-revision-history">
                {revisions.map((revision, index) => (
                  <div
                    key={revision.revision_id}
                    className={`rounded-md border p-2.5 ${
                      index === 0
                        ? 'border-blue-300 bg-blue-50 dark:border-blue-500/40 dark:bg-blue-500/10'
                        : 'border-gray-200 dark:border-gray-700'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-gray-900 dark:text-white">
                        v{revision.semantic_version}
                      </span>
                      <span className="text-[10px] text-gray-500">
                        #{revision.revision_number}
                      </span>
                    </div>
                    <div className="mt-1 truncate text-[11px] text-gray-600 dark:text-gray-300">
                      {revision.title}
                    </div>
                    <div className="mt-1 text-[10px] text-gray-500">
                      {revision.rules.length} rule
                      {revision.rules.length === 1 ? '' : 's'}
                    </div>
                  </div>
                ))}
              </div>
              {nextCursor && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void loadOlder()}
                  className="mt-3 w-full rounded-md border border-gray-300 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  {loadingMore ? 'Loading…' : 'Load older revisions'}
                </button>
              )}
              {paginationError && (
                <div
                  role="alert"
                  className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                >
                  <p>{paginationError}</p>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void loadFirstPage()}
                    className="mt-2 rounded border border-current px-2 py-1 font-semibold"
                  >
                    Restart history
                  </button>
                </div>
              )}
            </div>

            <button
              type="button"
              disabled={
                busy
                || Boolean(initialLoadError)
                || !canRetire
                || retirementStatus !== null
              }
              title={
                canRetire
                  ? retirementStatus
                    ? `Guideline is already ${retirementStatus}`
                    : 'Retire this guideline without deleting history'
                  : 'Requires guidelines.revisions.retire'
              }
              onClick={() => {
                setMutationError(null);
                setRetirementOpen(true);
              }}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-md border border-amber-300 px-3 py-2 text-xs font-semibold text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-amber-500/40 dark:text-amber-200 dark:hover:bg-amber-500/10"
            >
              <Archive size={14} />
              {retirementStatus
                ? `Guideline ${retirementStatus}`
                : 'Retire guideline'}
            </button>
          </aside>

          <main className="min-w-0 overflow-y-auto p-6">
            {loading && (
              <div className="py-16 text-center text-sm text-gray-500">
                Loading immutable revisions…
              </div>
            )}
            {initialLoadError && (
              <div
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
              >
                <div className="font-semibold">Revision editor unavailable</div>
                <p className="mt-1">{initialLoadError}</p>
                <p className="mt-2 text-xs">
                  Mutation controls remain disabled until the authoritative
                  revision history loads successfully.
                </p>
                <button
                  type="button"
                  onClick={() => void loadFirstPage()}
                  className="mt-3 rounded border border-current px-3 py-1.5 text-xs font-semibold"
                >
                  Retry
                </button>
              </div>
            )}

            {!loading && !initialLoadError && latest && (
              <div className="space-y-5">
                {!canCreate && (
                  <div
                    role="status"
                    className="rounded-lg border border-gray-200 bg-white p-3 text-sm text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                  >
                    Revision history is read-only. Creating a revision requires
                    guidelines.revisions.create.
                  </div>
                )}
                {canCreate && !canAuthorRules && (
                  <div
                    role="status"
                    className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                  >
                    Text and tags can be revised. Executable rules are read-only
                    without guidelines.rules.author_blocking.
                  </div>
                )}
                <section>
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                        New revision
                      </h3>
                      <p className="mt-1 text-sm text-gray-500">
                        The server classifies the minimum SemVer bump and
                        rejects an under-bump. Leave the version blank to use
                        the server-selected minimum.
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={busy || !canCreate}
                      onClick={() => resetDraft(latest)}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-white disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-900"
                    >
                      Reset draft
                    </button>
                  </div>

                  <div className="mt-4 grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
                    <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                      Title
                      <input
                        value={title}
                        disabled={busy || !canCreate}
                        onChange={(event) => setTitle(event.target.value)}
                        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                      />
                    </label>
                    <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                      Declared semantic version
                      <input
                        value={declaredVersion}
                        disabled={busy || !canCreate}
                        onChange={(event) => setDeclaredVersion(event.target.value)}
                        placeholder="Server minimum"
                        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                      />
                    </label>
                  </div>
                  <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-200">
                    Context content
                    <textarea
                      value={content}
                      disabled={busy || !canCreate}
                      onChange={(event) => setContent(event.target.value)}
                      rows={8}
                      className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                    />
                  </label>
                  <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-200">
                    Tags
                    <input
                      value={tags}
                      disabled={busy || !canCreate}
                      onChange={(event) => setTags(event.target.value)}
                      placeholder="comma, separated"
                      className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                    />
                  </label>
                </section>

                <section>
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h3 className="text-base font-semibold text-gray-900 dark:text-white">
                        Executable rules
                      </h3>
                      <p className="mt-1 text-xs text-gray-500">
                        New rules start advisory. Blocking must be selected
                        explicitly and unsupported combinations fail closed on
                        the server.
                      </p>
                    </div>
                    <button
                      type="button"
                      data-testid="add-policy-rule"
                      disabled={busy || !canAuthorRules}
                      onClick={() => setRules((current) => [...current, newRuleDraft()])}
                      className="inline-flex items-center gap-1 rounded-md bg-blue-600 px-3 py-2 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
                    >
                      <Plus size={13} />
                      Add rule
                    </button>
                  </div>

                  <div className="mt-3 space-y-3">
                    {rules.length === 0 ? (
                      <div className="rounded-lg border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500 dark:border-gray-700">
                        Context only. This revision has no executable rules.
                      </div>
                    ) : (
                      rules.map((rule, index) => (
                        <RuleEditorCard
                          key={rule.localId}
                          rule={rule}
                          index={index}
                          disabled={busy || !canAuthorRules}
                          onChange={(next) => updateRule(index, next)}
                          onRemove={() =>
                            setRules((current) =>
                              current.filter(
                                (_, currentIndex) => currentIndex !== index,
                              ),
                            )
                          }
                        />
                      ))
                    )}
                  </div>
                </section>

                <section className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
                  <div className="text-xs font-semibold uppercase text-gray-500">
                    Change summary
                  </div>
                  {changeSummary.length > 0 ? (
                    <ul className="mt-2 flex flex-wrap gap-2">
                      {changeSummary.map((change) => (
                        <li
                          key={change}
                          className="rounded bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700 dark:bg-blue-500/15 dark:text-blue-200"
                        >
                          {change}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 text-sm text-gray-500">
                      No changes from v{latest.semantic_version}.
                    </p>
                  )}
                  {ruleError && (
                    <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                      {ruleError}
                    </p>
                  )}
                  {tagError && (
                    <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                      {tagError}
                    </p>
                  )}
                  {mutationError && (
                    <div
                      role="alert"
                      className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
                    >
                      {mutationError}
                    </div>
                  )}
                  {mutationResult && (
                    <div
                      role="status"
                      className="mt-3 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-700 dark:border-green-500/30 dark:bg-green-500/10 dark:text-green-200"
                    >
                      {mutationResult}
                    </div>
                  )}
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      data-testid="create-guideline-revision"
                      disabled={
                        busy
                        || !canCreate
                        || changeSummary.length === 0
                        || Boolean(ruleError)
                        || Boolean(tagError)
                        || (
                          changeSummary.includes('executable rules')
                          && !canAuthorRules
                        )
                        || !title.trim()
                        || !content.trim()
                      }
                      onClick={() => void saveRevision()}
                      className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {saving ? 'Creating revision…' : 'Create immutable revision'}
                    </button>
                  </div>
                </section>
              </div>
            )}
          </main>
        </div>
      </div>

      {retirementOpen && (
        <RetirementDialog
          guidelineTitle={guideline.title}
          successorOptions={successorOptions}
          busy={saving}
          error={mutationError}
          onCancel={() => {
            if (!saving) {
              setMutationError(null);
              setRetirementOpen(false);
            }
          }}
          onConfirm={(request) => void retire(request)}
        />
      )}
    </div>
  );
}
