import { useEffect, useId, useMemo, useState } from 'react';

import type { SpecStatus } from '@/types';

import { SpecChecklistPanel } from './SpecChecklistPanel';
import { SpecValidationHistoryPanel } from './SpecValidationHistoryPanel';
import { isSpecValidationAvailable } from './specValidationAvailability';
import { QualityPanel } from '@/components/quality';
import {
  PolicyCompliancePanel,
  PolicyComplianceTransitionPreview,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
} from '@/components/policy-compliance';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { resolveSpecSemanticAnchor } from './specSemanticAnchors';

type ValidationSubTab =
  | 'checklist'
  | 'spec-validation'
  | 'requirement-lint'
  | 'policy-compliance';

interface SpecValidationPanelProps {
  boardId: string;
  specId: string;
  specVersion: number;
  specStatus: SpecStatus;
  canReadChecklist: boolean;
  canExecuteChecklist: boolean;
  canReadValidation: boolean;
  canReadQuality: boolean;
  canReadPolicyCompliance: boolean;
  /** Requirement text by stable child id, quoted inside lint findings. */
  anchorTexts?: Record<string, string>;
  policyTransitionPreview: PolicyTransitionPreviewLoadState;
  policyTransitionRejection?: PolicyTransitionRejection | null;
  specArchived: boolean;
  validationHistoryRefreshKey?: number;
  onAssessmentRecorded?: () => void;
  onPolicyEvaluated?: () => void;
  onOpenRequirementLintHelp?: () => void;
}

function preferredValidationTab(
  specStatus: SpecStatus,
  availableTabs: ValidationSubTab[],
): ValidationSubTab {
  const preference: ValidationSubTab[] =
    specStatus === 'approved'
      ? [
          'checklist',
          'spec-validation',
          'requirement-lint',
          'policy-compliance',
        ]
      : ['validated', 'in_progress', 'done'].includes(specStatus)
        ? [
            'spec-validation',
            'checklist',
            'requirement-lint',
            'policy-compliance',
          ]
        : [
            'requirement-lint',
            'checklist',
            'spec-validation',
            'policy-compliance',
          ];
  return preference.find((tab) => availableTabs.includes(tab))
    ?? availableTabs[0]
    ?? 'requirement-lint';
}

export function SpecValidationPanel({
  boardId,
  specId,
  specVersion,
  specStatus,
  canReadChecklist,
  canExecuteChecklist,
  canReadValidation,
  canReadQuality,
  canReadPolicyCompliance,
  anchorTexts,
  policyTransitionPreview,
  policyTransitionRejection = null,
  specArchived,
  validationHistoryRefreshKey = 0,
  onAssessmentRecorded,
  onPolicyEvaluated,
  onOpenRequirementLintHelp,
}: SpecValidationPanelProps) {
  const validationStageAvailable = isSpecValidationAvailable(specStatus);
  const tabs = useMemo<{
    id: ValidationSubTab;
    label: string;
    advisory?: boolean;
  }[]>(() => [
    ...(validationStageAvailable && canReadChecklist
      ? [{ id: 'checklist' as const, label: 'Checklist' }]
      : []),
    ...(validationStageAvailable && canReadValidation
      ? [{ id: 'spec-validation' as const, label: 'Spec Validation' }]
      : []),
    ...(canReadQuality
      ? [{
          id: 'requirement-lint' as const,
          label: 'Requirement lint',
          advisory: true,
        }]
      : []),
    ...(canReadPolicyCompliance
      ? [{
          id: 'policy-compliance' as const,
          label: 'Policy Compliance',
        }]
      : []),
  ], [
    canReadChecklist,
    canReadPolicyCompliance,
    canReadQuality,
    canReadValidation,
    validationStageAvailable,
  ]);
  const availableTabs = useMemo(
    () => tabs.map((tab) => tab.id),
    [tabs],
  );
  const [activeTab, setActiveTab] = useState<ValidationSubTab>(() =>
    preferredValidationTab(specStatus, availableTabs),
  );
  const tabIdPrefix = useId();
  const resolveSemanticAnchor = useMemo(
    () => (
      anchor: Parameters<typeof resolveSpecSemanticAnchor>[0],
    ) => resolveSpecSemanticAnchor(anchor, anchorTexts),
    [anchorTexts],
  );

  useEffect(() => {
    if (!availableTabs.includes(activeTab)) {
      setActiveTab(preferredValidationTab(specStatus, availableTabs));
    }
  }, [activeTab, availableTabs, specStatus]);

  if (tabs.length === 0) {
    return null;
  }

  return (
    <div className="space-y-4 p-4">
      <AccessibleTabList
        idBase={tabIdPrefix}
        ariaLabel="Spec validation sections"
        items={tabs.map((tab) => ({
          id: tab.id,
          label: tab.advisory ? (
            <>
              {tab.label}
              <span className="ml-1.5 rounded-full bg-blue-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-blue-700 dark:bg-blue-900/40 dark:text-blue-200">
                Advisory
              </span>
            </>
          ) : tab.label,
        }))}
        value={activeTab}
        onValueChange={setActiveTab}
        variant="secondary"
      />

      {activeTab === 'checklist' && canReadChecklist && (
        <AccessibleTabPanel
          idBase={tabIdPrefix}
          tabId="checklist"
          value={activeTab}
        >
          <SpecChecklistPanel
            boardId={boardId}
            specId={specId}
            expectedSpecVersion={specVersion}
            canRead={canReadChecklist}
            canExecute={
              specStatus === 'approved' && canExecuteChecklist
            }
            showHistory
          />
        </AccessibleTabPanel>
      )}

      {activeTab === 'spec-validation' && canReadValidation && (
        <AccessibleTabPanel
          idBase={tabIdPrefix}
          tabId="spec-validation"
          value={activeTab}
        >
          <SpecValidationHistoryPanel
            specId={specId}
            refreshKey={validationHistoryRefreshKey}
          />
        </AccessibleTabPanel>
      )}

      {activeTab === 'requirement-lint' && canReadQuality && (
        <AccessibleTabPanel
          idBase={tabIdPrefix}
          tabId="requirement-lint"
          value={activeTab}
        >
          <QualityPanel
            subjectType="spec"
            subjectId={specId}
            subjectVersion={specVersion}
            subjectStatus={specStatus}
            subjectArchived={specArchived}
            canRead={canReadQuality}
            canAssess={false}
            canProposeQuestions={false}
            anchorTexts={anchorTexts}
            onAssessmentRecorded={onAssessmentRecorded}
            onOpenHelp={onOpenRequirementLintHelp}
          />
        </AccessibleTabPanel>
      )}

      {activeTab === 'policy-compliance' && canReadPolicyCompliance && (
        <AccessibleTabPanel
          idBase={tabIdPrefix}
          tabId="policy-compliance"
          value={activeTab}
          className="space-y-4"
        >
          <PolicyComplianceTransitionPreview
            preview={policyTransitionPreview}
            rejection={policyTransitionRejection}
          />
          <PolicyCompliancePanel
            boardId={boardId}
            entityType="spec"
            subjectId={specId}
            subjectVersion={specVersion}
            transitionPreview={policyTransitionPreview}
            refreshKey={specVersion}
            resolveSemanticAnchor={resolveSemanticAnchor}
            onEvaluated={onPolicyEvaluated}
            onRefreshed={onPolicyEvaluated}
          />
        </AccessibleTabPanel>
      )}
    </div>
  );
}
