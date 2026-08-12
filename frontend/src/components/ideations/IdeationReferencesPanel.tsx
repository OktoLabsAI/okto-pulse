import { BookOpen, FileText, Layers } from 'lucide-react';
import type { ReactNode } from 'react';

import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { SpecEditionLabel } from '@/components/specs/SpecEditionLabel';
import {
  SPEC_STATUS_LABELS,
  STORY_STATUS_LABELS,
  type Ideation,
  type RefinementSummary,
  type SpecSummary,
  type StorySummary,
} from '@/types';

const STORY_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  triage: 'bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-300',
  ready: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  converted: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
};

const REFINEMENT_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  review: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  done: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
};

const SPEC_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  review: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  validated: 'bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300',
  in_progress: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
};

interface ReferenceSectionProps {
  title: string;
  count: number;
  emptyMessage: string;
  testId: string;
  children: ReactNode;
}

function ReferenceSection({
  title,
  count,
  emptyMessage,
  testId,
  children,
}: ReferenceSectionProps) {
  return (
    <section
      className="space-y-2 rounded-xl border border-gray-200 p-3 dark:border-gray-700"
      data-testid={testId}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
          {title}
        </h3>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">
          {count}
        </span>
      </div>
      {count === 0 ? (
        <p className="rounded-lg border border-dashed border-gray-200 px-3 py-4 text-center text-xs text-gray-400 dark:border-gray-700 dark:text-gray-500">
          {emptyMessage}
        </p>
      ) : children}
    </section>
  );
}

function StoryReference({
  story,
  onOpen,
}: {
  story: StorySummary;
  onOpen?: () => void;
}) {
  const content = (
    <>
      <div className="flex min-w-0 items-start gap-2">
        <BookOpen size={14} className="mt-0.5 shrink-0 text-blue-500" />
        <div className="min-w-0 text-left">
          <p className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
            {story.title}
          </p>
          {story.description && (
            <p className="mt-1 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
              {story.description}
            </p>
          )}
        </div>
      </div>
      <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs ${STORY_STATUS_COLORS[story.status] || ''}`}>
        {STORY_STATUS_LABELS[story.status]}
      </span>
    </>
  );

  return onOpen ? (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-start justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 hover:bg-blue-50 dark:bg-gray-700/40 dark:hover:bg-blue-950/20"
      data-testid={`ideation-reference-story-${story.id}`}
    >
      {content}
    </button>
  ) : (
    <div
      className="flex items-start justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 dark:bg-gray-700/40"
      data-testid={`ideation-reference-story-${story.id}`}
    >
      {content}
    </div>
  );
}

function RefinementReference({
  refinement,
  onOpen,
}: {
  refinement: RefinementSummary;
  onOpen?: () => void;
}) {
  const content = (
    <>
      <div className="flex min-w-0 items-center gap-2">
        <Layers size={14} className="shrink-0 text-violet-500" />
        <span className="truncate text-sm text-gray-700 dark:text-gray-300">
          {refinement.title}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-xs ${REFINEMENT_STATUS_COLORS[refinement.status] || ''}`}>
          {refinement.status.replace('_', ' ')}
        </span>
        <span className="text-[10px] text-gray-400">Edition {refinement.edition ?? 1}</span>
      </div>
    </>
  );

  return onOpen ? (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 hover:bg-violet-50 dark:bg-gray-700/40 dark:hover:bg-violet-950/20"
      data-testid={`ideation-reference-refinement-${refinement.id}`}
    >
      {content}
    </button>
  ) : (
    <div
      className="flex items-center justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 dark:bg-gray-700/40"
      data-testid={`ideation-reference-refinement-${refinement.id}`}
    >
      {content}
    </div>
  );
}

function SpecReference({
  spec,
  onOpen,
}: {
  spec: SpecSummary;
  onOpen?: () => void;
}) {
  const content = (
    <>
      <div className="flex min-w-0 items-center gap-2">
        <FileText size={14} className="shrink-0 text-teal-500" />
        <span className="truncate text-sm text-gray-700 dark:text-gray-300">
          {spec.title}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-xs ${SPEC_STATUS_COLORS[spec.status] || ''}`}>
          {SPEC_STATUS_LABELS[spec.status]}
        </span>
        <SpecEditionLabel
          edition={spec.edition}
          technicalRevision={spec.version}
          className="text-[10px] text-gray-400"
        />
      </div>
    </>
  );

  return onOpen ? (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 hover:bg-teal-50 dark:bg-gray-700/40 dark:hover:bg-teal-950/20"
      data-testid={`ideation-reference-spec-${spec.id}`}
    >
      {content}
    </button>
  ) : (
    <div
      className="flex items-center justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2.5 dark:bg-gray-700/40"
      data-testid={`ideation-reference-spec-${spec.id}`}
    >
      {content}
    </div>
  );
}

function getDirectIdeationSpecs(ideation: Ideation): SpecSummary[] {
  return (ideation.specs || []).filter((spec) => spec.refinement_id === null);
}

export function IdeationReferencesPanel({ ideation }: { ideation: Ideation }) {
  const modalStack = useOptionalModalStack();
  const directSpecs = getDirectIdeationSpecs(ideation);

  return (
    <div className="space-y-4" data-testid="ideation-references-panel">
      <div>
        <h2 className="text-base font-semibold text-gray-900 dark:text-white">
          References
        </h2>
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Related stories and the entities derived directly from this ideation.
        </p>
      </div>

      <ReferenceSection
        title="Related stories"
        count={ideation.stories?.length || 0}
        emptyMessage="No related stories"
        testId="ideation-reference-stories"
      >
        <div className="space-y-2">
          {(ideation.stories || []).map((story) => (
            <StoryReference
              key={story.id}
              story={story}
              onOpen={modalStack
                ? () => modalStack.push({ type: 'story', id: story.id })
                : undefined}
            />
          ))}
        </div>
      </ReferenceSection>

      <ReferenceSection
        title="Derived refinements"
        count={ideation.refinements?.length || 0}
        emptyMessage="No refinements derived from this ideation"
        testId="ideation-reference-refinements"
      >
        <div className="space-y-2">
          {(ideation.refinements || []).map((refinement) => (
            <RefinementReference
              key={refinement.id}
              refinement={refinement}
              onOpen={modalStack
                ? () => modalStack.push({ type: 'refinement', id: refinement.id })
                : undefined}
            />
          ))}
        </div>
      </ReferenceSection>

      <ReferenceSection
        title="Direct specs"
        count={directSpecs.length}
        emptyMessage="No spec derived directly from this ideation"
        testId="ideation-reference-specs"
      >
        <div className="space-y-2">
          {directSpecs.map((spec) => (
            <SpecReference
              key={spec.id}
              spec={spec}
              onOpen={modalStack
                ? () => modalStack.push({ type: 'spec', id: spec.id })
                : undefined}
            />
          ))}
        </div>
      </ReferenceSection>
    </div>
  );
}
