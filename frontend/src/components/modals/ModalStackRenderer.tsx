/**
 * ModalStackRenderer — renders the top entry of the modal stack.
 *
 * Ideação c13f7bd3. Lives at the root of the app (inside
 * ModalStackProvider) and:
 *
 *   - Picks the correct entity modal based on `stack.top.type`.
 *   - Passes `onClose={clear}` so that every modal's own X control
 *     collapses the whole stack ("X fecha todas as modais, independente
 *     do nível de drill down").
 *   - When `stack.length > 1`, overlays a small "← Back (N)" pill in
 *     the top-left that calls `pop()`. It sits above the modal backdrop
 *     (z-60) so the user always sees it, regardless of the underlying
 *     modal's layout.
 *
 * Each entity modal remains self-contained; the renderer is a thin
 * dispatcher + floating control, so no modal needed structural changes
 * to participate in the stack.
 */

import { ArrowLeft } from 'lucide-react';
import { useModalStack } from '@/contexts/ModalStackContext';
import { CardModal } from '@/components/kanban/CardModal';
import { SpecModal } from '@/components/specs/SpecModal';
import { IdeationModal } from '@/components/ideations/IdeationModal';
import { RefinementModal } from '@/components/refinements/RefinementModal';
import { SprintModal } from '@/components/sprints/SprintModal';
import { NodeDetailModal } from '@/components/knowledge/NodeDetailModal';
import { useDashboardStore } from '@/store/dashboard';

interface Props {
  /** Current board id. Required by most entity modals to scope lookups. */
  boardId: string;
}

export function ModalStackRenderer({ boardId }: Props) {
  const { stack, pop, clear } = useModalStack();
  const openCardInStore = useDashboardStore((s) => s.openCardModal);
  const closeCardInStore = useDashboardStore((s) => s.closeCardModal);
  const top = stack[stack.length - 1];
  if (!top) return null;

  // Clear the stack AND sync the dashboard store for card-type modals so
  // that navigating back to the tasks tab reflects the closed state.
  const handleClose = () => {
    if (top.type === 'card') closeCardInStore();
    clear();
  };

  // The pop handler also resyncs the card store when we're leaving a
  // card layer; otherwise the CardModal stays visually "open" in the
  // tasks tab even after the user drilled back.
  const handleBack = () => {
    if (top.type === 'card') closeCardInStore();
    pop();
    // If the newly-revealed entry is a card, let the store know so
    // CardModal re-opens for that id.
    const next = stack[stack.length - 2];
    if (next && next.type === 'card') {
      openCardInStore(next.id);
    }
  };

  return (
    <>
      {/* Back pill — only when we have somewhere to go back to. */}
      {stack.length > 1 && (
        <button
          type="button"
          onClick={handleBack}
          data-testid="modal-stack-back"
          className="fixed top-4 left-4 z-[60] inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 shadow-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 text-xs font-medium"
          title={`Back to previous (${stack.length - 1} layer${stack.length - 1 === 1 ? '' : 's'} behind)`}
        >
          <ArrowLeft size={14} />
          Back
          <span className="text-gray-400 dark:text-gray-500">
            ({stack.length - 1})
          </span>
        </button>
      )}

      {/* Dispatch on the top of the stack. A render-key forces React to
          remount the modal when the top changes to a different id of the
          same type — otherwise the modal's internal useEffect that
          fetches by id might keep stale state. */}
      {top.type === 'card' && (
        <CardModal key={`card-${top.id}`} boardId={boardId} onClose={handleClose} />
      )}
      {top.type === 'spec' && (
        <SpecModal
          key={`spec-${top.id}`}
          specId={top.id}
          boardId={boardId}
          onClose={handleClose}
          onChanged={() => {
            /* drill-down is read-only from here */
          }}
        />
      )}
      {top.type === 'ideation' && (
        <IdeationModal
          key={`ideation-${top.id}`}
          ideationId={top.id}
          boardId={boardId}
          onClose={handleClose}
          onChanged={() => {
            /* drill-down is read-only from here */
          }}
        />
      )}
      {top.type === 'refinement' && (
        <RefinementModal
          key={`refinement-${top.id}`}
          refinementId={top.id}
          boardId={boardId}
          onClose={handleClose}
          onChanged={() => {
            /* drill-down is read-only from here */
          }}
        />
      )}
      {top.type === 'sprint' && (
        <SprintModal
          key={`sprint-${top.id}`}
          sprintId={top.id}
          onClose={handleClose}
        />
      )}
      {top.type === 'kg_node' && (
        <NodeDetailModal
          key={`kg-${top.id}`}
          boardId={boardId}
          nodeId={top.id}
          onClose={handleClose}
        />
      )}
    </>
  );
}
