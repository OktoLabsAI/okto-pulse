/**
 * NodeDetailPanel — sidebar showing full details of a selected KG node.
 * Shows: type badge, title, content, confidence, validation, source link, actions.
 * Action buttons are wired to real API calls.
 */

import { useState } from 'react';
import toast from 'react-hot-toast';
import type { KGNode } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import * as kgApi from '@/services/kg-api';
import { RelevanceBadge } from './RelevanceBadge';

interface Props {
  node: KGNode;
  boardId: string;
  onClose: () => void;
  onNodeNavigate?: (nodeId: string) => void;
}

interface SimilarResult {
  id: string;
  title: string;
  similarity: number;
  combined_score: number;
}

interface ChainNode {
  id: string;
  title: string;
  created_at?: string;
  superseded_by?: string;
}

export function NodeDetailPanel({ node, boardId, onClose, onNodeNavigate }: Props) {
  const config = NODE_TYPE_CONFIG[node.node_type] || NODE_TYPE_CONFIG.Decision;
  const [similar, setSimilar] = useState<SimilarResult[] | null>(null);
  const [chain, setChain] = useState<ChainNode[] | null>(null);
  const [loadingSimilar, setLoadingSimilar] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);

  async function handleFindSimilar() {
    if (!node.title) return;
    setLoadingSimilar(true);
    setSimilar(null);
    setChain(null);
    try {
      const data = await kgApi.findSimilar(boardId, node.title, 10);
      const filtered = data.results.filter(r => r.id !== node.id);
      setSimilar(filtered);
      if (filtered.length === 0) {
        toast('No similar nodes found', { icon: 'i' });
      }
    } catch (err: any) {
      toast.error(err.message || 'Failed to find similar nodes');
    } finally {
      setLoadingSimilar(false);
    }
  }

  async function handleShowHistory() {
    setLoadingHistory(true);
    setChain(null);
    setSimilar(null);
    try {
      const data = await kgApi.getSupersedenceChain(boardId, node.id);
      setChain(data.chain);
      if (data.chain.length === 0) {
        toast('No supersedence history found', { icon: 'i' });
      }
    } catch (err: any) {
      toast.error(err.message || 'Failed to load history');
    } finally {
      setLoadingHistory(false);
    }
  }

  const [boosting, setBoosting] = useState(false);
  const [optimisticScore, setOptimisticScore] = useState<number | null>(null);

  async function handleBoost() {
    setBoosting(true);
    const before = typeof optimisticScore === 'number' ? optimisticScore : node.relevance_score ?? 0.5;
    const optimistic = Math.min(1.5, before + 0.3);
    setOptimisticScore(optimistic);
    try {
      const data = await kgApi.boostNode(boardId, node.id);
      setOptimisticScore(data.score_after);
      toast.success(`Score boosted: ${data.score_before.toFixed(2)} → ${data.score_after.toFixed(2)}`);
    } catch (err: any) {
      setOptimisticScore(before);
      toast.error(err.message || 'Failed to boost node');
    } finally {
      setBoosting(false);
    }
  }

  const displayScore =
    typeof optimisticScore === 'number' ? optimisticScore : node.relevance_score ?? 0.5;

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

      {/* Always show content section, even if empty */}
      <section className="mb-4">
        <h4 className="text-xs font-medium text-gray-500 uppercase mb-1">Content</h4>
        {node.content ? (
          <p className="text-sm text-gray-700 dark:text-gray-300">{node.content}</p>
        ) : (
          <p className="text-sm text-gray-400 italic dark:text-gray-500">No content available</p>
        )}
      </section>

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
          <span className="text-gray-500">Relevance</span>
          <div className="mt-1 flex items-center justify-between gap-2">
            <RelevanceBadge score={displayScore} compact />
            <button
              type="button"
              onClick={handleBoost}
              disabled={boosting || displayScore >= 1.5}
              className="px-2 py-0.5 text-xs rounded bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-400 text-white font-medium"
              title="Adds +0.3 to the relevance score (clamped at 1.5)"
            >
              {boosting ? '...' : 'Boost'}
            </button>
          </div>
          {typeof node.query_hits === 'number' && (
            <p className="mt-1 text-[10px] text-gray-500">
              hits: {node.query_hits}
            </p>
          )}
        </div>
      </div>

      {/* Always show node type and ID for debugging */}
      <div className="mb-4 text-xs bg-gray-50 dark:bg-gray-800 p-2 rounded">
        <div className="text-gray-500">Node Type</div>
        <div className="font-mono text-gray-900 dark:text-gray-100">{node.node_type}</div>
        <div className="text-gray-500 mt-2">Node ID</div>
        <div className="font-mono text-xs text-gray-600 dark:text-gray-400 break-all">{node.id}</div>
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

      {node.superseded_by && (
        <div className="mt-2 text-xs text-yellow-600 dark:text-yellow-400">
          Superseded by: {node.superseded_by.slice(0, 16)}...
        </div>
      )}

      <div className="mt-4 flex flex-col gap-2">
        <button
          onClick={handleFindSimilar}
          disabled={loadingSimilar}
          className="w-full px-3 py-1.5 text-xs bg-blue-50 text-blue-700 rounded hover:bg-blue-100 dark:bg-blue-900/20 dark:text-blue-400 disabled:opacity-50"
        >
          {loadingSimilar ? 'Searching...' : 'Find Similar'}
        </button>
        {node.node_type === 'Decision' && (
          <button
            onClick={handleShowHistory}
            disabled={loadingHistory}
            className="w-full px-3 py-1.5 text-xs bg-gray-50 text-gray-700 rounded hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-400 disabled:opacity-50"
          >
            {loadingHistory ? 'Loading...' : 'Show History'}
          </button>
        )}
      </div>

      {/* Similar nodes results */}
      {similar && similar.length > 0 && (
        <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3">
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
            Similar Nodes ({similar.length})
          </h4>
          <div className="space-y-1.5">
            {similar.map((s) => (
              <button
                key={s.id}
                onClick={() => onNodeNavigate?.(s.id)}
                className="w-full text-left p-2 rounded text-xs hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-100 dark:border-gray-800"
              >
                <div className="font-medium text-gray-900 dark:text-gray-100 truncate">
                  {s.title}
                </div>
                <div className="text-gray-400 mt-0.5">
                  {Math.round(s.similarity * 100)}% similar | Score: {s.combined_score}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Supersedence chain results */}
      {chain && chain.length > 0 && (
        <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3">
          <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
            Supersedence Chain ({chain.length})
          </h4>
          <div className="space-y-1">
            {chain.map((c, i) => (
              <div key={c.id} className="flex items-start gap-2">
                <div className="flex flex-col items-center">
                  <div className="w-2 h-2 rounded-full bg-violet-500 mt-1.5" />
                  {i < chain.length - 1 && (
                    <div className="w-px h-6 bg-violet-300 dark:bg-violet-700" />
                  )}
                </div>
                <button
                  onClick={() => onNodeNavigate?.(c.id)}
                  className="text-left text-xs hover:underline"
                >
                  <span className="text-gray-900 dark:text-gray-100">{c.title}</span>
                  {c.created_at && (
                    <span className="text-gray-400 ml-1">
                      ({new Date(c.created_at).toLocaleDateString()})
                    </span>
                  )}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
