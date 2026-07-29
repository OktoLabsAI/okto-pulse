import { useEffect, useId, useState } from 'react';

import type { SpecStatus } from '@/types';

import { SpecChecklistPanel } from './SpecChecklistPanel';
import { SpecValidationHistoryPanel } from './SpecValidationHistoryPanel';
import { isSpecValidationAvailable } from './specValidationAvailability';

type ValidationSubTab = 'checklist' | 'spec-validation';

interface SpecValidationPanelProps {
  boardId: string;
  specId: string;
  specVersion: number;
  specStatus: SpecStatus;
  canReadChecklist: boolean;
  canExecuteChecklist: boolean;
  canReadValidation: boolean;
  validationHistoryRefreshKey?: number;
}

export function SpecValidationPanel({
  boardId,
  specId,
  specVersion,
  specStatus,
  canReadChecklist,
  canExecuteChecklist,
  canReadValidation,
  validationHistoryRefreshKey = 0,
}: SpecValidationPanelProps) {
  const [activeTab, setActiveTab] = useState<ValidationSubTab>(
    canReadChecklist ? 'checklist' : 'spec-validation',
  );
  const tabIdPrefix = useId();

  useEffect(() => {
    if (
      activeTab === 'checklist' &&
      !canReadChecklist &&
      canReadValidation
    ) {
      setActiveTab('spec-validation');
    } else if (
      activeTab === 'spec-validation' &&
      !canReadValidation &&
      canReadChecklist
    ) {
      setActiveTab('checklist');
    }
  }, [activeTab, canReadChecklist, canReadValidation]);

  if (
    !isSpecValidationAvailable(specStatus) ||
    (!canReadChecklist && !canReadValidation)
  ) {
    return null;
  }

  const tabs: { id: ValidationSubTab; label: string }[] = [
    ...(canReadChecklist
      ? [{ id: 'checklist' as const, label: 'Checklist' }]
      : []),
    ...(canReadValidation
      ? [{ id: 'spec-validation' as const, label: 'Spec Validation' }]
      : []),
  ];

  return (
    <div className="space-y-4 p-4">
      <div
        className="inline-flex rounded-lg border border-surface-200 bg-surface-50 p-1 dark:border-surface-700 dark:bg-surface-900"
        role="tablist"
        aria-label="Spec validation sections"
      >
        {tabs.map((tab) => {
          const selected = activeTab === tab.id;
          const tabId = `${tabIdPrefix}-${tab.id}-tab`;
          const panelId = `${tabIdPrefix}-${tab.id}-panel`;

          return (
            <button
              key={tab.id}
              id={tabId}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={panelId}
              onClick={() => setActiveTab(tab.id)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                selected
                  ? 'bg-white text-blue-700 shadow-sm dark:bg-surface-700 dark:text-blue-200'
                  : 'text-surface-500 hover:text-surface-800 dark:text-surface-400 dark:hover:text-surface-100'
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {activeTab === 'checklist' && canReadChecklist && (
        <section
          id={`${tabIdPrefix}-checklist-panel`}
          role="tabpanel"
          aria-labelledby={`${tabIdPrefix}-checklist-tab`}
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
        </section>
      )}

      {activeTab === 'spec-validation' && canReadValidation && (
        <section
          id={`${tabIdPrefix}-spec-validation-panel`}
          role="tabpanel"
          aria-labelledby={`${tabIdPrefix}-spec-validation-tab`}
        >
          <SpecValidationHistoryPanel
            specId={specId}
            refreshKey={validationHistoryRefreshKey}
          />
        </section>
      )}
    </div>
  );
}
