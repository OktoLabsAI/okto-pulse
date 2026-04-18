/**
 * NodePreviewPanel — floating inline preview in the upper-left of the
 * graph canvas (Spec 8 / AC-8, Sprint 5 S5.2).
 *
 * Triggered by a *single-click* selection — distinct from the full
 * NodeDetailPanel which opens on the right sidebar only on double-click.
 * The preview keeps the user in the graph view and offers two exits:
 *   - Close (X): deselects the node via props.onClose
 *   - Open in spec: navigates to /specs/{source_artifact_ref} when the
 *     node carries a spec reference (buttons are suppressed otherwise)
 */

import { useMemo } from 'react';
import type { KGNode } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

interface Props {
  node: KGNode | null;
  onClose: () => void;
  onOpenSpec?: (specRef: string) => void;
  /** Called when the user clicks "Show more" — promotes the inline preview
   *  to a full NodeDetailModal rendered by the parent. */
  onShowDetails?: (node: KGNode) => void;
}

const SPEC_REF_PATTERN = /^spec:/i;

export function NodePreviewPanel({ node, onClose, onOpenSpec, onShowDetails }: Props) {
  const specRef = useMemo(() => {
    if (!node?.source_artifact_ref) return null;
    return SPEC_REF_PATTERN.test(node.source_artifact_ref)
      ? node.source_artifact_ref.replace(SPEC_REF_PATTERN, '')
      : null;
  }, [node?.source_artifact_ref]);

  if (!node) return null;
  const cfg = NODE_TYPE_CONFIG[node.node_type];

  return (
    <aside
      data-testid="kg-preview-panel"
      role="dialog"
      aria-label={`Preview of ${node.title}`}
      className="absolute top-4 left-4 z-30 w-80 rounded-md bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 shadow-xl border border-gray-200 dark:border-gray-700 p-3"
    >
      <div className="flex items-start justify-between mb-2">
        <span
          className="px-2 py-0.5 rounded text-[10px] font-medium text-white"
          style={{ backgroundColor: cfg?.color ?? '#6B7280' }}
        >
          {cfg?.icon ?? ''} {node.node_type}
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
          <div className="text-gray-500">Validation</div>
          <div className="font-semibold">{node.validation_status}</div>
        </div>
      </div>

      {node.source_artifact_ref && (
        <div className="text-[11px] mb-2">
          <span className="text-gray-500">Source: </span>
          <span className="font-mono text-blue-600 dark:text-blue-400 break-all">
            {node.source_artifact_ref}
          </span>
        </div>
      )}

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
        {specRef && onOpenSpec && (
          <button
            type="button"
            onClick={() => onOpenSpec(specRef)}
            data-testid="kg-preview-open-spec"
            className="w-full px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Open in spec
          </button>
        )}
      </div>
    </aside>
  );
}
