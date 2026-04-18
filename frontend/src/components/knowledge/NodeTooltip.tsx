/**
 * NodeTooltip — floating hover card rendered at the bottom-left of the
 * graph canvas (Spec 8 / AC-7, Sprint 5 S5.1).
 *
 * The tooltip is driven by the `hoveredNode` prop — the parent wires
 * React Flow's onNodeMouseEnter / onNodeMouseLeave handlers to set / clear
 * the hovered node, so this component only owns its presentation.
 *
 * AC-7 timing: the spec requires the tooltip to appear in <=100ms after
 * mouse-enter. We don't debounce the display at all on our side — the
 * state update propagates in a single render tick, well under the cap.
 */

import type { KGNode } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

interface Props {
  node: KGNode | null;
}

const CONTENT_TRUNCATE = 200;

export function NodeTooltip({ node }: Props) {
  if (!node) return null;
  const cfg = NODE_TYPE_CONFIG[node.node_type];
  const preview = node.content && node.content.length > CONTENT_TRUNCATE
    ? `${node.content.slice(0, CONTENT_TRUNCATE)}…`
    : node.content;

  return (
    <div
      data-testid="kg-node-tooltip"
      role="tooltip"
      aria-label={`Tooltip for ${node.title}`}
      className="absolute bottom-4 left-4 z-20 max-w-sm rounded-md bg-gray-900/95 text-white shadow-lg p-3 text-xs pointer-events-none"
    >
      <div className="flex items-center gap-2 mb-1">
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: cfg?.color ?? '#9CA3AF' }}
          aria-hidden="true"
        />
        <span className="font-medium truncate">{node.title}</span>
      </div>
      <div className="text-gray-300 flex flex-wrap gap-x-3 gap-y-0.5">
        <span>{node.node_type}</span>
        <span>
          conf {(node.source_confidence * 100).toFixed(0)}%
        </span>
        {node.source_artifact_ref && (
          <span className="font-mono truncate max-w-[16rem]" title={node.source_artifact_ref}>
            {node.source_artifact_ref}
          </span>
        )}
      </div>
      {preview && (
        <p className="mt-2 text-gray-200 leading-snug" data-testid="kg-node-tooltip-content">
          {preview}
        </p>
      )}
    </div>
  );
}
