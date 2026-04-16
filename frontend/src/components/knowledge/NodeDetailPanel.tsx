/**
 * NodeDetailPanel — sidebar showing full details of a selected KG node.
 * Shows: type badge, title, content, confidence, validation, source link, actions.
 */

import type { KGNode } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

interface Props {
  node: KGNode;
  onClose: () => void;
}

export function NodeDetailPanel({ node, onClose }: Props) {
  const config = NODE_TYPE_CONFIG[node.node_type] || NODE_TYPE_CONFIG.Decision;

  return (
    <div className="p-4" role="complementary" aria-label="Node detail panel">
      <div className="flex items-center justify-between mb-4">
        <span
          className="px-2 py-1 rounded text-xs font-medium text-white"
          style={{ backgroundColor: config.color }}
        >
          {config.icon} {node.node_type}
        </span>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          aria-label="Close panel"
        >
          ESC
        </button>
      </div>

      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
        {node.title}
      </h3>

      {node.content && (
        <section className="mb-4">
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Content</h4>
          <p className="text-sm text-gray-700 dark:text-gray-300">{node.content}</p>
        </section>
      )}

      {node.justification && (
        <section className="mb-4">
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Justification</h4>
          <p className="text-sm text-gray-700 dark:text-gray-300">{node.justification}</p>
        </section>
      )}

      <div className="grid grid-cols-2 gap-2 mb-4 text-xs">
        <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded">
          <span className="text-gray-500">Confidence</span>
          <p className="font-semibold text-gray-900 dark:text-gray-100">
            {(node.source_confidence * 100).toFixed(0)}%
          </p>
        </div>
        <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded">
          <span className="text-gray-500">Validation</span>
          <p className="font-semibold text-gray-900 dark:text-gray-100">
            {node.validation_status}
          </p>
        </div>
      </div>

      {node.source_artifact_ref && (
        <div className="mb-4 text-xs">
          <span className="text-gray-500">Source: </span>
          <span className="font-mono text-blue-600 dark:text-blue-400">
            {node.source_artifact_ref}
          </span>
        </div>
      )}

      {node.created_at && (
        <div className="text-xs text-gray-400">
          Created: {new Date(node.created_at).toLocaleDateString()}
        </div>
      )}

      <div className="mt-4 flex flex-col gap-2">
        <button className="w-full px-3 py-1.5 text-xs bg-blue-50 text-blue-700 rounded hover:bg-blue-100 dark:bg-blue-900/20 dark:text-blue-400">
          Find Similar
        </button>
        <button className="w-full px-3 py-1.5 text-xs bg-gray-50 text-gray-700 rounded hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-400">
          Show History
        </button>
      </div>
    </div>
  );
}
