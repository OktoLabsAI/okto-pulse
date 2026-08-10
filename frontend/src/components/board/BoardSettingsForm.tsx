// Shared board-settings form. This is the single source of truth for the
// validation-gate / coverage / governance layout so the Board Config screen
// (Menu > Board, board-level mutation) and the Global Default template editor
// (Menu > Board > Global Default, immutable versioning) render EXACTLY the same
// disposition and controls. The only difference is the persistence callback:
//   - Board Config passes updateSettings (PATCH board.settings)
//   - Global Default passes createActiveTemplateVersion (new template version)
// Both receive a Partial<BoardSettings> patch from the same controls.
import { type ReactNode, useEffect, useState } from 'react';
import { BookOpen, ClipboardCheck, Fingerprint, Image, Info, Languages, Network, Palette, Shield, SlidersHorizontal, Users } from 'lucide-react';
import { normalizeRefinementAmbiguityThreshold } from '@/components/board/refinementAmbiguitySettings';
import {
  normalizeReviewerSeparationMode,
  REVIEWER_SEPARATION_MODES,
} from '@/components/board/reviewerSeparationSettings';
import type {
  BoardSettings,
  CodeTraceabilitySettings,
  LintLanguageCode,
  ReviewerSeparationMode,
  SpecResourceAutoDeriveType,
} from '@/types';

const DEFAULT_CODE_TRACEABILITY_SETTINGS: CodeTraceabilitySettings = {
  mode: 'off',
  evidence_attestation: 'preferred',
  target_resolution: 'advisory',
  accepted_attestor_policy: 'granular_permission',
  minimum_trust: 'single_attestation',
  preflight_freshness_seconds: 1800,
  overlap_policy: 'warn',
  observed_state_policy: 'allow_dirty_attestation',
  receipt_content: 'safe_excerpt',
};

interface SettingsToggleProps {
  checked: boolean;
  onChange: () => void;
  ariaLabel: string;
  activeColor?: 'amber' | 'violet';
  testId?: string;
}

export function SettingsToggle({
  checked,
  onChange,
  ariaLabel,
  activeColor = 'violet',
  testId,
}: SettingsToggleProps) {
  const activeClass = activeColor === 'amber' ? 'bg-amber-500' : 'bg-violet-500';

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      data-testid={testId}
      onClick={onChange}
      className={`relative inline-flex h-5 w-10 shrink-0 items-center rounded-full p-0.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900 ${checked ? activeClass : 'bg-gray-300 dark:bg-gray-600'}`}
    >
      <span
        className={`h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${checked ? 'translate-x-5' : 'translate-x-0'}`}
      />
    </button>
  );
}

function SettingsSection({
  title,
  description,
  icon,
  children,
  className = '',
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900/40 ${className}`}>
      <div className="mb-3">
        <h4 className="flex items-center gap-1.5 text-xs font-semibold text-gray-800 dark:text-gray-100">
          {icon}
          {title}
        </h4>
        {description && (
          <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
            {description}
          </p>
        )}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function SettingRow({
  label,
  description,
  children,
}: {
  label: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
          {label}
        </label>
        <p className="mt-0.5 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
          {description}
        </p>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function SettingsSelect({
  ariaLabel,
  value,
  onChange,
  children,
}: {
  ariaLabel: string;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <select
      aria-label={ariaLabel}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="max-w-52 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs text-gray-800 shadow-sm outline-none transition focus:border-violet-400 focus:ring-2 focus:ring-violet-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:focus:ring-violet-900"
    >
      {children}
    </select>
  );
}

const SPEC_RESOURCE_OPTIONS: Array<{
  type: SpecResourceAutoDeriveType;
  label: string;
  title: string;
  Icon: typeof BookOpen;
}> = [
  { type: 'knowledge_base', label: 'KB', title: 'Knowledge Base', Icon: BookOpen },
  { type: 'architecture', label: 'Arch', title: 'Architecture', Icon: Network },
  { type: 'mockup', label: 'Mockup', title: 'Mockup', Icon: Image },
];

const DEFAULT_SPEC_RESOURCE_TYPES = SPEC_RESOURCE_OPTIONS.map((option) => option.type);

const LINT_LANGUAGE_OPTIONS: Array<{
  code: LintLanguageCode;
  label: string;
  title: string;
}> = [
  { code: 'pt-BR', label: 'PT-BR', title: 'Português (Brasil)' },
  { code: 'en-US', label: 'EN-US', title: 'English (US)' },
  { code: 'es-ES', label: 'ES', title: 'Español' },
  { code: 'de-DE', label: 'DE', title: 'Deutsch' },
  { code: 'fr-FR', label: 'FR', title: 'Français' },
];
export const IMPACT_EVIDENCE_MODES = ['off', 'advisory', 'require'] as const;
export type ImpactEvidenceMode = (typeof IMPACT_EVIDENCE_MODES)[number];

function normalizeImpactEvidenceMode(value: unknown): ImpactEvidenceMode {
  // Mirrors the backend resolver: anything unknown reads as 'off' instead of
  // failing the screen (invalid_value_fail_compat).
  return IMPACT_EVIDENCE_MODES.includes(value as ImpactEvidenceMode)
    ? (value as ImpactEvidenceMode)
    : 'off';
}

export const DESIGN_SYSTEM_GATE_MODES = ['off', 'advisory', 'blocking'] as const;
export type DesignSystemGateMode = (typeof DESIGN_SYSTEM_GATE_MODES)[number];

export function normalizeDesignSystemGateMode(value: unknown): DesignSystemGateMode {
  return DESIGN_SYSTEM_GATE_MODES.includes(value as DesignSystemGateMode)
    ? (value as DesignSystemGateMode)
    : 'off';
}

type NumericSettingKey =
  | 'min_confidence'
  | 'min_completeness'
  | 'max_drift'
  | 'min_spec_completeness'
  | 'min_spec_assertiveness'
  | 'max_spec_ambiguity';

export interface BoardSettingsFormProps {
  settings: BoardSettings;
  onChange: (patch: Partial<BoardSettings>) => void;
  /**
   * Board-config-only context warnings (empty description / empty guidelines).
   * Rendered inside the Agent Governance section. The Global Default template
   * editor passes nothing — a template has no board to warn about.
   */
  contextWarnings?: ReactNode;
}

export function BoardSettingsForm({ settings, onChange, contextWarnings }: BoardSettingsFormProps) {
  // Local draft state for numeric gate inputs — committed on blur to avoid
  // refresh-on-keystroke wiping partial values while the user is typing.
  const [minConfidenceDraft, setMinConfidenceDraft] = useState<string>(String(settings.min_confidence));
  const [minCompletenessDraft, setMinCompletenessDraft] = useState<string>(String(settings.min_completeness));
  const [maxDriftDraft, setMaxDriftDraft] = useState<string>(String(settings.max_drift));
  const [minSpecCompletenessDraft, setMinSpecCompletenessDraft] = useState<string>(String(settings.min_spec_completeness ?? 80));
  const [minSpecAssertivenessDraft, setMinSpecAssertivenessDraft] = useState<string>(String(settings.min_spec_assertiveness ?? 80));
  const [maxSpecAmbiguityDraft, setMaxSpecAmbiguityDraft] = useState<string>(String(settings.max_spec_ambiguity ?? 30));
  useEffect(() => { setMinConfidenceDraft(String(settings.min_confidence)); }, [settings.min_confidence]);
  useEffect(() => { setMinCompletenessDraft(String(settings.min_completeness)); }, [settings.min_completeness]);
  useEffect(() => { setMaxDriftDraft(String(settings.max_drift)); }, [settings.max_drift]);
  useEffect(() => { setMinSpecCompletenessDraft(String(settings.min_spec_completeness ?? 80)); }, [settings.min_spec_completeness]);
  useEffect(() => { setMinSpecAssertivenessDraft(String(settings.min_spec_assertiveness ?? 80)); }, [settings.min_spec_assertiveness]);
  useEffect(() => { setMaxSpecAmbiguityDraft(String(settings.max_spec_ambiguity ?? 30)); }, [settings.max_spec_ambiguity]);

  const autoDeriveEnabled = settings.auto_derive_spec_resources_enabled ?? false;
  const autoDeriveResourceTypes = settings.auto_derive_spec_resource_types ?? [];

  const toggleSpecResourceAutomation = () => {
    const next = !autoDeriveEnabled;
    onChange({
      auto_derive_spec_resources_enabled: next,
      auto_derive_spec_resource_types:
        next && autoDeriveResourceTypes.length === 0
          ? DEFAULT_SPEC_RESOURCE_TYPES
          : autoDeriveResourceTypes,
    });
  };

  const toggleSpecResourceType = (resourceType: SpecResourceAutoDeriveType) => {
    if (!autoDeriveEnabled) return;
    const selected = autoDeriveResourceTypes.includes(resourceType);
    if (selected && autoDeriveResourceTypes.length === 1) return;
    const nextTypes = selected
      ? autoDeriveResourceTypes.filter((item) => item !== resourceType)
      : [...autoDeriveResourceTypes, resourceType];
    onChange({ auto_derive_spec_resource_types: nextTypes });
  };

  const lintLanguages = settings.lint_languages ?? [];
  const codeTraceability = {
    ...DEFAULT_CODE_TRACEABILITY_SETTINGS,
    ...(settings.code_traceability ?? {}),
  };

  const updateCodeTraceability = (
    patch: Partial<CodeTraceabilitySettings>,
  ) => {
    onChange({
      code_traceability: {
        ...codeTraceability,
        ...patch,
      },
    });
  };

  const toggleLintLanguage = (code: LintLanguageCode) => {
    const next = lintLanguages.includes(code)
      ? lintLanguages.filter((item) => item !== code)
      : [...lintLanguages, code];
    onChange({ lint_languages: next });
  };

  const commitNumericSetting = (key: NumericSettingKey, raw: string) => {
    const parsed = Math.min(100, Math.max(0, Number(raw)));
    const current = (settings[key] ?? 0) as number;
    const safe = Number.isFinite(parsed) ? parsed : current;
    if (safe === current) {
      // Re-sync draft in case of invalid input (e.g. empty string)
      if (key === 'min_confidence') setMinConfidenceDraft(String(safe));
      if (key === 'min_completeness') setMinCompletenessDraft(String(safe));
      if (key === 'max_drift') setMaxDriftDraft(String(safe));
      if (key === 'min_spec_completeness') setMinSpecCompletenessDraft(String(safe));
      if (key === 'min_spec_assertiveness') setMinSpecAssertivenessDraft(String(safe));
      if (key === 'max_spec_ambiguity') setMaxSpecAmbiguityDraft(String(safe));
      return;
    }
    onChange({ [key]: safe } as Partial<BoardSettings>);
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <SettingsSection
        title="General"
        description="Board-level limits used across validation flows."
        icon={<SlidersHorizontal size={12} />}
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
            Max test scenarios per card
          </label>
          <p className="mb-2 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
            Limits how many scenarios a single card can be linked to.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {[1, 2, 3, 5, 10].map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => onChange({ max_scenarios_per_card: n })}
                className={`h-8 w-8 rounded text-xs font-medium transition-colors ${
                  settings.max_scenarios_per_card === n
                    ? 'bg-blue-500 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                }`}
              >
                {n}
              </button>
            ))}
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Requirement Lint Languages"
        description="Languages the deterministic requirement lint analyzes with native lexicons. Requirements are checked against the union of the selected languages; with none selected, only language-neutral signals (numbers, comparators, units) are detected."
        icon={<Languages size={12} />}
      >
        <div className="grid grid-cols-5 gap-2" aria-label="Requirement lint languages" data-testid="lint-languages">
          {LINT_LANGUAGE_OPTIONS.map(({ code, label, title }) => {
            const checked = lintLanguages.includes(code);
            return (
              <button
                key={code}
                type="button"
                title={title}
                aria-pressed={checked}
                data-testid={`lint-language-${code}`}
                onClick={() => toggleLintLanguage(code)}
                className={`flex h-9 min-w-0 items-center justify-center rounded border px-2 text-[11px] font-medium transition-colors ${
                  checked
                    ? 'border-violet-400 bg-violet-50 text-violet-700 dark:border-violet-500/70 dark:bg-violet-500/15 dark:text-violet-200'
                    : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                }`}
              >
                <span className="truncate">{label}</span>
              </button>
            );
          })}
        </div>
      </SettingsSection>

      <SettingsSection
        title="Coverage Overrides"
        description="Global bypasses for board coverage rules."
        icon={<Shield size={12} />}
      >
        <SettingRow label="Skip test coverage" description="Bypass test coverage checks for all specs.">
          <SettingsToggle
            checked={settings.skip_test_coverage_global}
            onChange={() => onChange({ skip_test_coverage_global: !settings.skip_test_coverage_global })}
            ariaLabel="Skip test coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip rules coverage" description="Bypass FR to BR coverage checks for all specs.">
          <SettingsToggle
            checked={settings.skip_rules_coverage_global}
            onChange={() => onChange({ skip_rules_coverage_global: !settings.skip_rules_coverage_global })}
            ariaLabel="Skip rules coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip TRs coverage" description="Bypass TR to Task coverage checks for all specs.">
          <SettingsToggle
            checked={settings.skip_trs_coverage_global}
            onChange={() => onChange({ skip_trs_coverage_global: !settings.skip_trs_coverage_global })}
            ariaLabel="Skip TRs coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip contract coverage" description="Bypass API contract to Task coverage checks.">
          <SettingsToggle
            checked={settings.skip_contract_coverage_global}
            onChange={() => onChange({ skip_contract_coverage_global: !settings.skip_contract_coverage_global })}
            ariaLabel="Skip contract coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip IR coverage" description="Bypass Integration Requirement to Task coverage checks.">
          <SettingsToggle
            checked={settings.skip_ir_coverage_global}
            onChange={() => onChange({ skip_ir_coverage_global: !settings.skip_ir_coverage_global })}
            ariaLabel="Skip IR coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip OR coverage" description="Bypass Observability Requirement to Task coverage checks.">
          <SettingsToggle
            checked={settings.skip_or_coverage_global}
            onChange={() => onChange({ skip_or_coverage_global: !settings.skip_or_coverage_global })}
            ariaLabel="Skip OR coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip task requirement link gate" description="Allow task cards to start without a direct FR/TR/BR/IR/OR link.">
          <SettingsToggle
            checked={settings.skip_task_requirement_link_gate_global ?? false}
            onChange={() => onChange({ skip_task_requirement_link_gate_global: !(settings.skip_task_requirement_link_gate_global ?? false) })}
            ariaLabel="Skip task requirement link gate"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip decisions coverage" description="Bypass active Decision to Task linkage for all specs.">
          <SettingsToggle
            checked={settings.skip_decisions_coverage_global}
            onChange={() => onChange({ skip_decisions_coverage_global: !settings.skip_decisions_coverage_global })}
            ariaLabel="Skip decisions coverage"
            activeColor="amber"
          />
        </SettingRow>
        <SettingRow label="Skip cognitive closeout" description="Allow done transitions even when cognitive consolidation is pending. Badges and KG Health pending lists remain visible.">
          <SettingsToggle
            checked={settings.skip_cognitive_consolidation ?? false}
            onChange={() => onChange({ skip_cognitive_consolidation: !(settings.skip_cognitive_consolidation ?? false) })}
            ariaLabel="Skip cognitive closeout"
            activeColor="amber"
            testId="toggle-skip-cognitive-closeout"
          />
        </SettingRow>
        <SettingRow label="Skip evidence requirement" description="Bypass evidence required to mark scenarios passed, automated or failed.">
          <SettingsToggle
            checked={settings.skip_test_evidence_global ?? false}
            onChange={() => {
              const next = !(settings.skip_test_evidence_global ?? false);
              onChange({ skip_test_evidence_global: next });
            }}
            ariaLabel="Skip evidence requirement"
            activeColor="amber"
            testId="toggle-skip-evidence"
          />
        </SettingRow>
      </SettingsSection>

      <SettingsSection
        title="Agent Governance"
        description="Controls agent autonomy and context requirements for critical actions."
        icon={<Users size={12} />}
        className="lg:col-span-2"
      >
        <div className="grid gap-3 lg:grid-cols-2">
          <SettingRow label="Allow agent self-answering" description="Agents may answer their own Q&A questions when board policy permits it.">
            <SettingsToggle
              checked={settings.allow_agent_self_answering ?? false}
              onChange={() => onChange({ allow_agent_self_answering: !(settings.allow_agent_self_answering ?? false) })}
              ariaLabel="Allow agent self-answering"
              testId="toggle-agent-self-answering"
            />
          </SettingRow>
          <SettingRow label="Require full context for critical actions" description="Status changes, validations, approvals and closeout require full entity context.">
            <SettingsToggle
              checked={settings.require_full_context_for_critical_actions ?? true}
              onChange={() => onChange({ require_full_context_for_critical_actions: !(settings.require_full_context_for_critical_actions ?? true) })}
              ariaLabel="Require full context for critical actions"
              testId="toggle-full-context-critical-actions"
            />
          </SettingRow>
        </div>

        <div>
          <div className="mb-2">
            <p
              id="reviewer-separation-mode-label"
              className="block text-xs font-medium text-gray-700 dark:text-gray-300"
            >
              Independent reviewer policy
            </p>
            <p
              id="reviewer-separation-mode-description"
              className="mt-0.5 text-[10px] leading-4 text-gray-400 dark:text-gray-500"
            >
              Controls whether a task creator, assignee or executor may submit its validation.
            </p>
          </div>
          <div
            className="grid grid-cols-3 gap-2"
            role="group"
            aria-labelledby="reviewer-separation-mode-label"
            aria-describedby="reviewer-separation-mode-description"
            data-testid="reviewer-separation-mode"
          >
            {REVIEWER_SEPARATION_MODES.map((mode) => {
              const checked = normalizeReviewerSeparationMode(
                settings.reviewer_separation_mode,
              ) === mode;
              const labels: Record<ReviewerSeparationMode, string> = {
                off: 'Off',
                warn: 'Warn',
                enforce: 'Enforce',
              };
              const titles: Record<ReviewerSeparationMode, string> = {
                off: 'Allow validation while retaining conflict details in the audit trail.',
                warn: 'Allow validation and persist a reviewer-separation warning.',
                enforce: 'Block validation when the reviewer created, owns or executed the task.',
              };
              return (
                <button
                  key={mode}
                  type="button"
                  title={titles[mode]}
                  aria-pressed={checked}
                  data-testid={`reviewer-separation-mode-${mode}`}
                  onClick={() => onChange({ reviewer_separation_mode: mode })}
                  className={`h-9 rounded border px-2 text-[11px] font-medium transition-colors ${
                    checked
                      ? 'border-violet-400 bg-violet-50 text-violet-700 dark:border-violet-500/70 dark:bg-violet-500/15 dark:text-violet-200'
                      : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {labels[mode]}
                </button>
              );
            })}
          </div>
          <p className="mt-2 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
            Enforce is the safe default for new boards. Legacy boards without a stored value remain Off until explicitly changed.
          </p>
        </div>

        {contextWarnings}
      </SettingsSection>

      <SettingsSection
        title="Agent-mediated Code Traceability"
        description="Policy for structured observations submitted by authenticated external agents."
        icon={<Fingerprint size={12} />}
        className="lg:col-span-2"
      >
        <div
          className="flex items-start gap-2 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2.5 text-[11px] leading-4 text-sky-900 dark:border-sky-900/70 dark:bg-sky-950/30 dark:text-sky-200"
          role="note"
          data-testid="code-traceability-source-blind-disclosure"
        >
          <Info size={14} className="mt-0.5 shrink-0" aria-hidden />
          <p>
            Pulse does not access source code. An authenticated external agent
            checks access and capabilities in its own environment, then submits
            an <strong>accessible</strong>, <strong>partial</strong>, or{' '}
            <strong>unavailable</strong> attestation. Pulse Community only
            accepts, stores, and presents those receipts.
          </p>
        </div>

        <div className="grid gap-x-8 gap-y-3 lg:grid-cols-2">
          <SettingRow
            label="Enforcement mode"
            description="Keep existing boards off, show guidance, or block gated transitions."
          >
            <SettingsSelect
              ariaLabel="Code Traceability enforcement mode"
              value={codeTraceability.mode}
              onChange={(value) => updateCodeTraceability({
                mode: value as CodeTraceabilitySettings['mode'],
              })}
            >
              <option value="off">Off</option>
              <option value="advisory">Advisory</option>
              <option value="blocking">Blocking</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Accepted attestors"
            description="Require granular agent permission, with an optional board allowlist."
          >
            <SettingsSelect
              ariaLabel="Accepted attestor policy"
              value={codeTraceability.accepted_attestor_policy}
              onChange={(value) => updateCodeTraceability({
                accepted_attestor_policy:
                  value as CodeTraceabilitySettings['accepted_attestor_policy'],
              })}
            >
              <option value="granular_permission">Permission</option>
              <option value="granular_permission_and_board_allowlist">
                Permission + allowlist
              </option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Evidence receipt requirement"
            description="Controls whether Code Evidence needs a current accepted agent receipt."
          >
            <SettingsSelect
              ariaLabel="Evidence receipt requirement"
              value={codeTraceability.evidence_attestation}
              onChange={(value) => updateCodeTraceability({
                evidence_attestation:
                  value as CodeTraceabilitySettings['evidence_attestation'],
              })}
            >
              <option value="none">None</option>
              <option value="preferred">Preferred</option>
              <option value="required">Required</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Target resolution receipt"
            description="Requires agent-attested target resolutions, optionally against the current receipt."
          >
            <SettingsSelect
              ariaLabel="Target resolution receipt requirement"
              value={codeTraceability.target_resolution}
              onChange={(value) => updateCodeTraceability({
                target_resolution:
                  value as CodeTraceabilitySettings['target_resolution'],
              })}
            >
              <option value="advisory">Advisory</option>
              <option value="required">Required</option>
              <option value="required_current_receipt">Required + current</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Receipt freshness"
            description="Maximum age of an accepted agent check receipt (60 seconds to 24 hours)."
          >
            <div className="flex items-center gap-1.5">
              <input
                aria-label="Receipt freshness in minutes"
                type="number"
                min={1}
                max={1440}
                value={Math.round(codeTraceability.preflight_freshness_seconds / 60)}
                onChange={(event) => {
                  const minutes = Math.max(
                    1,
                    Math.min(1440, Number(event.target.value) || 1),
                  );
                  updateCodeTraceability({
                    preflight_freshness_seconds: minutes * 60,
                  });
                }}
                className="w-20 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-right text-xs text-gray-800 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
              />
              <span className="text-[10px] text-gray-400">min</span>
            </div>
          </SettingRow>

          <SettingRow
            label="Minimum trust"
            description="Accept one agent attestation or require corroboration by another agent."
          >
            <SettingsSelect
              ariaLabel="Minimum receipt trust"
              value={codeTraceability.minimum_trust}
              onChange={(value) => updateCodeTraceability({
                minimum_trust: value as CodeTraceabilitySettings['minimum_trust'],
              })}
            >
              <option value="single_attestation">Single attestation</option>
              <option value="corroborated">Corroborated</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Overlap policy"
            description="Controls guidance when Tasks target the same agent-attested symbol or file."
          >
            <SettingsSelect
              ariaLabel="Implementation overlap policy"
              value={codeTraceability.overlap_policy}
              onChange={(value) => updateCodeTraceability({
                overlap_policy: value as CodeTraceabilitySettings['overlap_policy'],
              })}
            >
              <option value="off">Off</option>
              <option value="warn">Warn</option>
              <option value="block_parallel">Block parallel</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Observed state"
            description="Allow dirty-state attestations or require the agent to declare a committed state."
          >
            <SettingsSelect
              ariaLabel="Observed source state policy"
              value={codeTraceability.observed_state_policy}
              onChange={(value) => updateCodeTraceability({
                observed_state_policy:
                  value as CodeTraceabilitySettings['observed_state_policy'],
              })}
            >
              <option value="allow_dirty_attestation">Allow dirty attestation</option>
              <option value="require_committed_attestation">Require committed</option>
            </SettingsSelect>
          </SettingRow>

          <SettingRow
            label="Receipt content"
            description="Retain metadata only, or a defensively sanitized excerpt submitted by the agent."
          >
            <SettingsSelect
              ariaLabel="Receipt content retention"
              value={codeTraceability.receipt_content}
              onChange={(value) => updateCodeTraceability({
                receipt_content: value as CodeTraceabilitySettings['receipt_content'],
              })}
            >
              <option value="metadata_only">Metadata only</option>
              <option value="safe_excerpt">Safe excerpt</option>
            </SettingsSelect>
          </SettingRow>
        </div>

        <p className="border-t border-gray-100 pt-2 text-[10px] leading-4 text-gray-400 dark:border-gray-800 dark:text-gray-500">
          No repository binding, provider credential, local checkout, filesystem
          search, or source resolver is configured in Pulse Community.
        </p>
      </SettingsSection>

      <SettingsSection
        title="Task Validation Gate"
        description="Controls the gate before execution work can be completed."
        icon={<Shield size={12} />}
      >
        <SettingRow label="Require task validation" description="Tasks must pass validation before moving to Done.">
          <SettingsToggle
            checked={settings.require_task_validation}
            onChange={() => onChange({ require_task_validation: !settings.require_task_validation })}
            ariaLabel="Require task validation"
          />
        </SettingRow>

        {settings.require_task_validation && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Min Confidence
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={minConfidenceDraft}
                  data-testid="bsf-num-min_confidence"
                  onChange={(e) => setMinConfidenceDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('min_confidence', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Min Completeness
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={minCompletenessDraft}
                  data-testid="bsf-num-min_completeness"
                  onChange={(e) => setMinCompletenessDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('min_completeness', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Max Drift
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={maxDriftDraft}
                  data-testid="bsf-num-max_drift"
                  onChange={(e) => setMaxDriftDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('max_drift', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        title="Spec Validation Gate"
        description="Spec validation, resource coverage and automatic resource derivation."
        icon={<Shield size={12} />}
      >
        <SettingRow label="Auto-derive Spec resources" description="Copy selected resources to new or linked cards.">
          <SettingsToggle
            checked={autoDeriveEnabled}
            onChange={toggleSpecResourceAutomation}
            ariaLabel="Auto-derive Spec resources"
            testId="toggle-spec-resource-automation"
          />
        </SettingRow>

        <div className="grid grid-cols-3 gap-2" aria-label="Spec resource types">
          {SPEC_RESOURCE_OPTIONS.map(({ type, label, title, Icon }) => {
            const checked = autoDeriveResourceTypes.includes(type);
            const isLastSelected = autoDeriveEnabled && checked && autoDeriveResourceTypes.length === 1;
            return (
              <button
                key={type}
                type="button"
                title={title}
                aria-pressed={checked}
                aria-disabled={!autoDeriveEnabled || isLastSelected}
                data-testid={`spec-resource-type-${type}`}
                onClick={() => toggleSpecResourceType(type)}
                className={`flex h-9 min-w-0 items-center justify-center gap-1 rounded border px-2 text-[11px] font-medium transition-colors ${
                  checked
                    ? 'border-violet-400 bg-violet-50 text-violet-700 dark:border-violet-500/70 dark:bg-violet-500/15 dark:text-violet-200'
                    : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                } ${!autoDeriveEnabled ? 'cursor-not-allowed opacity-50' : ''} ${isLastSelected ? 'cursor-not-allowed' : ''}`}
              >
                <Icon size={12} className="shrink-0" />
                <span className="truncate">{label}</span>
              </button>
            );
          })}
        </div>

        <SettingRow label="Require resource-to-task coverage" description="Spec Architecture, Mockup and KB must be linked to tasks.">
          <SettingsToggle
            checked={settings.require_spec_resource_task_coverage !== false}
            onChange={() =>
              onChange({
                require_spec_resource_task_coverage: !(settings.require_spec_resource_task_coverage !== false),
              })
            }
            ariaLabel="Require resource-to-task coverage"
            testId="toggle-resource-task-coverage"
          />
        </SettingRow>

        <SettingRow label="Require spec validation" description="Specs must pass Completeness, Assertiveness and Ambiguity gates before Validated.">
          <SettingsToggle
            checked={settings.require_spec_validation ?? true}
            onChange={() => onChange({ require_spec_validation: !(settings.require_spec_validation ?? true) })}
            ariaLabel="Require spec validation"
          />
        </SettingRow>

        {settings.require_spec_validation && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Min Completeness
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={minSpecCompletenessDraft}
                  data-testid="bsf-num-min_spec_completeness"
                  onChange={(e) => setMinSpecCompletenessDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('min_spec_completeness', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Min Assertiveness
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={minSpecAssertivenessDraft}
                  data-testid="bsf-num-min_spec_assertiveness"
                  onChange={(e) => setMinSpecAssertivenessDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('min_spec_assertiveness', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-[10px] font-medium text-gray-500 dark:text-gray-400">
                Max Ambiguity
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={maxSpecAmbiguityDraft}
                  data-testid="bsf-num-max_spec_ambiguity"
                  onChange={(e) => setMaxSpecAmbiguityDraft(e.target.value)}
                  onBlur={(e) => commitNumericSetting('max_spec_ambiguity', e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                  className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                />
                <span className="text-[10px] text-gray-400">/ 100</span>
              </div>
            </div>
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        title="Execution Report Evidence"
        description="Controls declared impact evidence (files, symbols, surfaces, tests) on the execution report of a card."
        icon={<ClipboardCheck size={12} />}
      >
        <SettingRow
          label="Require declared impact evidence"
          description="Blocking mode rejects a gated move whose conclusion has no populated evidence section."
        >
          <SettingsToggle
            checked={normalizeImpactEvidenceMode(settings.impact_evidence_mode) === 'require'}
            onChange={() =>
              onChange({
                impact_evidence_mode:
                  normalizeImpactEvidenceMode(settings.impact_evidence_mode) === 'require'
                    ? 'off'
                    : 'require',
              })
            }
            ariaLabel="Require declared impact evidence"
            testId="toggle-impact-evidence-gate"
          />
        </SettingRow>

        <div
          className="grid grid-cols-3 gap-2"
          aria-label="Impact evidence mode"
          data-testid="impact-evidence-mode"
        >
          {IMPACT_EVIDENCE_MODES.map((mode) => {
            const checked = normalizeImpactEvidenceMode(settings.impact_evidence_mode) === mode;
            const labels: Record<ImpactEvidenceMode, string> = {
              off: 'Off',
              advisory: 'Advisory',
              require: 'Require',
            };
            const titles: Record<ImpactEvidenceMode, string> = {
              off: 'No effect on moves.',
              advisory: 'Gated moves succeed; a missing block is recorded in the activity log.',
              require: 'Gated moves are rejected without at least one populated section.',
            };
            return (
              <button
                key={mode}
                type="button"
                title={titles[mode]}
                aria-pressed={checked}
                data-testid={`impact-evidence-mode-${mode}`}
                onClick={() => onChange({ impact_evidence_mode: mode })}
                className={`h-9 rounded border px-2 text-[11px] font-medium transition-colors ${
                  checked
                    ? 'border-violet-400 bg-violet-50 text-violet-700 dark:border-violet-500/70 dark:bg-violet-500/15 dark:text-violet-200'
                    : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                }`}
              >
                {labels[mode]}
              </button>
            );
          })}
        </div>
        <p className="text-[10px] leading-4 text-gray-400 dark:text-gray-500">
          Test cards and validator-approved completions stay exempt in every mode.
        </p>
      </SettingsSection>

      <SettingsSection
        title="Design System Gate"
        description="Controls Design System consumption checks on mockup submissions."
        icon={<Palette size={12} />}
      >
        <SettingRow label="Require Design System evidence" description="Blocking mode requires every new mockup to reference the board's effective Design System with evidence.">
          <SettingsToggle
            checked={(settings.design_system_gate_mode ?? 'off') === 'blocking'}
            onChange={() =>
              onChange({
                design_system_gate_mode:
                  (settings.design_system_gate_mode ?? 'off') === 'blocking' ? 'off' : 'blocking',
              })
            }
            ariaLabel="Require Design System evidence"
            testId="toggle-design-system-gate"
          />
        </SettingRow>

        <div
          className="grid grid-cols-3 gap-2"
          aria-label="Design System gate mode"
          data-testid="design-system-gate-mode"
        >
          {DESIGN_SYSTEM_GATE_MODES.map((mode) => {
            const checked = (settings.design_system_gate_mode ?? 'off') === mode;
            const labels: Record<DesignSystemGateMode, string> = {
              off: 'Off',
              advisory: 'Advisory',
              blocking: 'Blocking',
            };
            return (
              <button
                key={mode}
                type="button"
                aria-pressed={checked}
                data-testid={`design-system-gate-mode-${mode}`}
                onClick={() => onChange({ design_system_gate_mode: mode })}
                className={`h-9 rounded border px-2 text-[11px] font-medium transition-colors ${
                  checked
                    ? 'border-violet-400 bg-violet-50 text-violet-700 dark:border-violet-500/70 dark:bg-violet-500/15 dark:text-violet-200'
                    : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                }`}
              >
                {labels[mode]}
              </button>
            );
          })}
        </div>
      </SettingsSection>

      <SettingsSection
        title="Ideation Ambiguity Gate"
        description="Block evaluating→done when an ideation's ambiguity score is missing or above the threshold."
        icon={<Shield size={12} />}
      >
        <SettingRow label="Require ideation ambiguity gate" description="Opt-in. Each ideation can still skip the gate individually.">
          <SettingsToggle
            checked={settings.require_ideation_ambiguity_gate ?? false}
            onChange={() => onChange({ require_ideation_ambiguity_gate: !(settings.require_ideation_ambiguity_gate ?? false) })}
            ariaLabel="Require ideation ambiguity gate"
            testId="toggle-ideation-ambiguity-gate"
          />
        </SettingRow>

        {(settings.require_ideation_ambiguity_gate ?? false) && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
              Max Ambiguity
            </label>
            <p className="mb-2 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
              Highest allowed ambiguity score before done is blocked.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              {[1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => onChange({ max_ideation_ambiguity: n })}
                  data-testid={`button-max-ideation-ambiguity-${n}`}
                  className={`h-8 w-8 rounded text-xs font-medium transition-colors ${
                    (settings.max_ideation_ambiguity ?? 3) === n
                      ? 'bg-blue-500 text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        title="Refinement Ambiguity Gate"
        description="Block approved→done unless the current refinement assessment meets the ambiguity threshold; this runs before Resource and Cognitive gates."
        icon={<Shield size={12} />}
      >
        <SettingRow
          label="Require refinement ambiguity gate"
          description="Opt-in. A human can record a reasoned skip on an individual refinement."
        >
          <SettingsToggle
            checked={settings.require_refinement_ambiguity_gate ?? false}
            onChange={() => onChange({
              require_refinement_ambiguity_gate: !(settings.require_refinement_ambiguity_gate ?? false),
            })}
            ariaLabel="Require refinement ambiguity gate"
            testId="toggle-refinement-ambiguity-gate"
          />
        </SettingRow>

        {(settings.require_refinement_ambiguity_gate ?? false) && (
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
              Max Ambiguity
            </label>
            <p className="mb-2 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
              Highest allowed ambiguity level in the current assessment.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              {[1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => onChange({ max_refinement_ambiguity: n })}
                  data-testid={`button-max-refinement-ambiguity-${n}`}
                  className={`h-8 w-8 rounded text-xs font-medium transition-colors ${
                    normalizeRefinementAmbiguityThreshold(settings.max_refinement_ambiguity) === n
                      ? 'bg-blue-500 text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>
        )}
      </SettingsSection>
    </div>
  );
}
