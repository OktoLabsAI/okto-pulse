/**
 * NodePreviewPanel — floating inline preview in the upper-left of the
 * graph canvas (Spec 8 / AC-8, Sprint 5 S5.2).
 *
 * Triggered by a *single-click* selection — distinct from the full
 * NodeDetailPanel which opens on the right sidebar only on double-click.
 * The preview keeps the user in the graph view and offers two exits:
 *   - Close (X): deselects the node via props.onClose
 *   - Open source: resolves the owning artifact and opens its existing modal.
 */

import type { KGNode } from '@/types/knowledge-graph';
import { kgNodeDisplayType, kgNodeVisualConfig } from '@/types/knowledge-graph';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { RelevanceBadge } from './RelevanceBadge';
import { NodeSourceLink } from './NodeSourceLink';
import { useOptionalModalStack } from '@/contexts/ModalStackContext';

interface Props {
  node: KGNode | null;
  onClose: () => void;
  boardId?: string;
  /** Called when the user clicks "Show more" — promotes the inline preview
   *  to a full NodeDetailModal rendered by the parent. */
  onShowDetails?: (node: KGNode) => void;
}

export function NodePreviewPanel({ node, onClose, boardId, onShowDetails }: Props) {
  const modalStack = useOptionalModalStack();
  useEscapeToClose(onClose, { enabled: Boolean(node) && !modalStack?.stack.length, priority: 10 });

  if (!node) return null;
  const cfg = kgNodeVisualConfig(node);

  return (
    <aside
      data-testid="kg-preview-panel"
      role="dialog"
      aria-label={`Preview of ${node.title}`}
      className="absolute top-4 left-4 z-30 w-80 max-w-[calc(100%-2rem)] max-h-[calc(100%-2rem)] overflow-y-auto rounded-md bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 shadow-xl border border-gray-200 dark:border-gray-700 p-3"
    >
      <div className="flex items-start justify-between mb-2">
        <span
          className="px-2 py-0.5 rounded text-[10px] font-medium text-white"
          style={{ backgroundColor: cfg?.color ?? '#6B7280' }}
        >
          {cfg?.icon ?? ''} {kgNodeDisplayType(node)}
        </span>
        <button
          type="button"
          onClick={onClose}
          data-testid="kg-preview-close"
          aria-label="Close preview"
          className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 -mt-1 -mr-1 px-1.5 py-0.5 rounded"
        >
          ×
        </button>
      </div>

      <h3 className="text-sm font-semibold leading-snug mb-2">{node.title}</h3>
      <NodeSourceLink node={node} boardId={boardId} />

      {node.content && (
        <p className="text-xs text-gray-700 dark:text-gray-300 mb-2 whitespace-pre-wrap">
          {node.content}
        </p>
      )}

      {node.justification && (
        <div className="mb-2">
          <div className="text-[10px] uppercase text-gray-500 mb-0.5">Justification</div>
          <p className="text-xs text-gray-700 dark:text-gray-300">{node.justification}</p>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 text-[11px] mb-2">
        <div className="bg-gray-50 dark:bg-gray-800 rounded px-2 py-1">
          <div className="text-gray-500">Confidence</div>
          <div className="font-semibold">
            {(node.source_confidence * 100).toFixed(0)}%
          </div>
        </div>
        <div className="bg-gray-50 dark:bg-gray-800 rounded px-2 py-1">
          <div className="text-gray-500">Relevance</div>
          <div className="mt-0.5">
            <RelevanceBadge score={node.relevance_score} compact />
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        {onShowDetails && (
          <button
            type="button"
            onClick={() => onShowDetails(node)}
            data-testid="kg-preview-show-more"
            className="w-full px-3 py-1.5 text-xs bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-200 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
          >
            Show more
          </button>
        )}
      </div>
    </aside>
  );
}
