import { useId } from 'react';
import {
  FileText,
  Layers,
  Lightbulb,
  Link2,
  Zap,
} from 'lucide-react';

import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { SpecEditionLabel } from '@/components/specs/SpecEditionLabel';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import {
  SPEC_STATUS_LABELS,
  type SpecStatus,
  type SpecSummary,
} from '@/types';

export type RefinementReferenceTab = 'ideation' | 'specs';

interface IdeationReference {
  id: string;
  title: string;
  version: number;
}

const SPEC_STATUS_COLORS: Record<SpecStatus, string> = {
  draft:
    'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  review:
    'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved:
    'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  validated:
    'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  in_progress:
    'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  done:
    'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled:
    'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

function SpecReference({
  spec,
  compact = false,
}: {
  spec: SpecSummary;
  compact?: boolean;
}) {
  const modalStack = useOptionalModalStack();
  return (
    <button
      type="button"
      onClick={() => modalStack?.push({ type: 'spec', id: spec.id })}
      className={`flex min-w-0 items-center gap-2 rounded-lg border border-gray-200 bg-white text-left transition-colors hover:border-blue-300 hover:bg-blue-50/40 dark:border-gray-700 dark:bg-gray-900/40 dark:hover:border-blue-700 dark:hover:bg-blue-950/20 ${
        compact ? 'max-w-[28rem] px-2 py-1' : 'w-full px-3 py-2.5'
      }`}
      aria-label={`Open spec ${spec.title}`}
      data-testid={`refinement-spec-reference-${spec.id}`}
    >
      <FileText size={14} className="shrink-0 text-violet-500" />
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-800 dark:text-gray-100">
        {spec.title}
      </span>
      <span
        className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${SPEC_STATUS_COLORS[spec.status]}`}
      >
        {SPEC_STATUS_LABELS[spec.status]}
      </span>
      <SpecEditionLabel
        edition={spec.edition}
        technicalRevision={spec.version}
        className="shrink-0 text-[10px] text-gray-400"
      />
      {spec.archived && (
        <span className="shrink-0 rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-700 dark:text-slate-200">
          Archived
        </span>
      )}
    </button>
  );
}

export function RefinementToSummary({
  specs,
  onSeeReferences,
}: {
  specs: SpecSummary[];
  onSeeReferences: () => void;
}) {
  if (specs.length === 0) {
    return (
      <span
        className="text-xs text-gray-400"
        data-testid="refinement-to-empty"
      >
        Not derived
      </span>
    );
  }
  if (specs.length === 1) {
    return <SpecReference spec={specs[0]} compact />;
  }
  return (
    <button
      type="button"
      onClick={onSeeReferences}
      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-blue-600 hover:bg-blue-50 hover:text-blue-700 dark:text-blue-400 dark:hover:bg-blue-950/30"
      data-testid="refinement-to-many"
    >
      <Layers size={12} />
      {specs.length} specs · See references
    </button>
  );
}

interface RefinementReferencesPanelProps {
  originId: string;
  origin: IdeationReference | null;
  specs: SpecSummary[];
  activeTab: RefinementReferenceTab;
  onTabChange: (tab: RefinementReferenceTab) => void;
  canDeriveSpec: boolean;
  derivingSpec: boolean;
  onCreateSpec: () => void;
}

export function RefinementReferencesPanel({
  originId,
  origin,
  specs,
  activeTab,
  onTabChange,
  canDeriveSpec,
  derivingSpec,
  onCreateSpec,
}: RefinementReferencesPanelProps) {
  const modalStack = useOptionalModalStack();
  const tabIdPrefix = useId();
  const tabs: {
    id: RefinementReferenceTab;
    label: string;
    count: number;
  }[] = [
    { id: 'ideation', label: 'Origin ideation', count: originId ? 1 : 0 },
    { id: 'specs', label: 'Derived specs', count: specs.length },
  ];

  return (
    <div className="space-y-4" data-testid="refinement-references-panel">
      <AccessibleTabList
        idBase={`${tabIdPrefix}-references`}
        ariaLabel="Refinement reference sections"
        items={tabs}
        value={activeTab}
        onValueChange={onTabChange}
        variant="secondary"
        className="max-w-full"
      />

      <AccessibleTabPanel
        idBase={`${tabIdPrefix}-references`}
        tabId="ideation"
        value={activeTab}
      >
          {originId ? (
            <button
              type="button"
              onClick={() =>
                modalStack?.push({ type: 'ideation', id: originId })
              }
              className="flex w-full items-center gap-3 rounded-lg border border-amber-200 bg-amber-50/50 px-3 py-3 text-left hover:border-amber-300 hover:bg-amber-50 dark:border-amber-800 dark:bg-amber-950/20 dark:hover:border-amber-700"
              aria-label={`Open ideation ${origin?.title || originId}`}
              data-testid="refinement-origin-reference"
            >
              <Lightbulb size={16} className="shrink-0 text-amber-500" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-amber-800 dark:text-amber-200">
                  {origin?.title || originId}
                </span>
                <span className="mt-0.5 block text-xs text-amber-600/80 dark:text-amber-300/70">
                  Source ideation
                </span>
              </span>
              {origin && (
                <span className="shrink-0 text-[10px] text-amber-600 dark:text-amber-300">
                  v{origin.version}
                </span>
              )}
            </button>
          ) : (
            <p className="rounded-lg border border-dashed border-gray-300 p-4 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
              No origin ideation is recorded.
            </p>
          )}
      </AccessibleTabPanel>

      <AccessibleTabPanel
        idBase={`${tabIdPrefix}-references`}
        tabId="specs"
        value={activeTab}
        className="space-y-3"
      >
          {specs.length === 0 ? (
            <div className="py-6 text-center">
              <Link2
                size={32}
                className="mx-auto mb-2 text-gray-300 dark:text-gray-600"
              />
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No derived specs
              </p>
              {canDeriveSpec && (
                <p className="mt-1 text-xs text-gray-400">
                  Create a structured Spec draft from this Refinement.
                </p>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {specs.map((spec) => (
                <SpecReference key={spec.id} spec={spec} />
              ))}
            </div>
          )}
          {canDeriveSpec && (
            <button
              type="button"
              onClick={onCreateSpec}
              disabled={derivingSpec}
              className="mt-3 flex items-center gap-1.5 text-sm text-indigo-600 hover:text-indigo-800 disabled:opacity-50 dark:text-indigo-400 dark:hover:text-indigo-300"
            >
              <Zap size={14} />
              {derivingSpec ? 'Creating...' : 'Create Spec Draft'}
            </button>
          )}
      </AccessibleTabPanel>
    </div>
  );
}
