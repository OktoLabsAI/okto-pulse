/**
 * KGValidationTab — displays the KG nodes/edges derived from a spec,
 * grouped by node type, with validation status counts.
 *
 * Data source: `kgApi.getSubgraph(boardId, { limit: 500 })` filtered by
 * `source_artifact_ref === "spec:{specId}"`. This is a best-effort view —
 * specs whose derived graph exceeds 500 nodes will be truncated. We surface
 * a notice when that happens and link to the full KG view as a follow-up.
 */

import { useEffect, useMemo, useState } from 'react';
import * as kgApi from '@/services/kg-api';
import { NODE_TYPE_CONFIG, type KGNode, type KGEdge, type KGNodeType } from '@/types/knowledge-graph';
import { NodeDetailModal } from '@/components/knowledge/NodeDetailModal';

interface Props {
  boardId: string;
  specId: string;
}

interface NodeTypeSummary {
  type: KGNodeType;
  total: number;
  validated: number;
  unvalidated: number;
  avgConfidence: number;
}

const SUBGRAPH_PAGE_SIZE = 500;

export function KGValidationTab({ boardId, specId }: Props) {
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [selected, setSelected] = useState<KGNode | null>(null);

  const artifactRef = `spec:${specId}`;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    kgApi
      .getSubgraph(boardId, { limit: SUBGRAPH_PAGE_SIZE })
      .then((resp) => {
        if (cancelled) return;
        const mine = (resp.nodes ?? []).filter(
          (n) => n.source_artifact_ref === artifactRef,
        );
        const myIds = new Set(mine.map((n) => n.id));
        const myEdges = (resp.edges ?? []).filter(
          (e) => myIds.has(e.source) || myIds.has(e.target),
        );
        setNodes(mine);
        setEdges(myEdges);
        setTruncated(resp.next_cursor !== null);
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? 'Failed to load KG subgraph');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [boardId, artifactRef]);

  const summaries: NodeTypeSummary[] = useMemo(() => {
    const buckets = new Map<KGNodeType, KGNode[]>();
    for (const n of nodes) {
      const list = buckets.get(n.node_type) ?? [];
      list.push(n);
      buckets.set(n.node_type, list);
    }
    return Array.from(buckets.entries())
      .map(([type, ns]) => {
        const validated = ns.filter((n) => n.validation_status !== 'unvalidated').length;
        const avg =
          ns.reduce((s, n) => s + (n.source_confidence ?? 0), 0) / (ns.length || 1);
        return {
          type,
          total: ns.length,
          validated,
          unvalidated: ns.length - validated,
          avgConfidence: avg,
        };
      })
      .sort((a, b) => b.total - a.total);
  }, [nodes]);

  const edgesByType = useMemo(() => {
    const buckets = new Map<string, number>();
    for (const e of edges) {
      buckets.set(e.edge_type, (buckets.get(e.edge_type) ?? 0) + 1);
    }
    return Array.from(buckets.entries()).sort((a, b) => b[1] - a[1]);
  }, [edges]);

  const totalValidated = summaries.reduce((s, x) => s + x.validated, 0);
  const totalUnvalidated = summaries.reduce((s, x) => s + x.unvalidated, 0);

  if (loading) {
    return (
      <div className="p-6 text-sm text-gray-500 animate-pulse" data-testid="kg-validation-loading">
        Loading validation data from Knowledge Graph…
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-sm text-red-500" data-testid="kg-validation-error">
        {error}
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <div className="p-6 text-sm text-gray-500 dark:text-gray-400" data-testid="kg-validation-empty">
        <p>
          No Knowledge Graph data for this spec yet. Once the consolidation
          worker processes the spec, derived nodes + edges will appear here.
        </p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6" data-testid="kg-validation-tab">
      {truncated && (
        <div className="rounded border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/40 px-3 py-2 text-xs text-amber-900 dark:text-amber-100">
          Showing the first {SUBGRAPH_PAGE_SIZE} nodes in the board. Some entries
          may be missing from this view.
        </div>
      )}

      <div className="grid grid-cols-3 gap-3">
        <Metric label="Nodes derived" value={nodes.length} />
        <Metric label="Validated" value={totalValidated} tone="ok" />
        <Metric label="Unvalidated" value={totalUnvalidated} tone={totalUnvalidated > 0 ? 'warn' : 'neutral'} />
      </div>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
          Nodes by type
        </h3>
        <div className="space-y-2">
          {summaries.map((s) => {
            const cfg = NODE_TYPE_CONFIG[s.type];
            return (
              <div
                key={s.type}
                className="border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 flex items-center gap-3"
                data-testid={`kg-validation-summary-${s.type}`}
              >
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide text-white shrink-0"
                  style={{ backgroundColor: cfg?.color ?? '#6B7280' }}
                >
                  <span>{cfg?.icon ?? ''}</span>
                  <span>{s.type}</span>
                </span>
                <div className="flex-1 grid grid-cols-3 gap-2 text-xs text-gray-700 dark:text-gray-300">
                  <span>
                    <span className="font-medium">{s.total}</span> total
                  </span>
                  <span>
                    <span className="font-medium text-green-700 dark:text-green-400">
                      {s.validated}
                    </span>{' '}
                    validated
                  </span>
                  <span className={s.unvalidated > 0 ? 'text-amber-700 dark:text-amber-400' : ''}>
                    <span className="font-medium">{s.unvalidated}</span> pending
                  </span>
                </div>
                <div className="text-xs text-gray-500">
                  conf {(s.avgConfidence * 100).toFixed(0)}%
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
          Edges by type ({edges.length})
        </h3>
        {edgesByType.length === 0 ? (
          <p className="text-xs text-gray-500">No edges extracted yet.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {edgesByType.map(([type, count]) => (
              <span
                key={type}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300"
              >
                <span className="font-mono">{type}</span>
                <span className="text-gray-500">·</span>
                <span className="font-semibold">{count}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
          All derived nodes
        </h3>
        <div className="space-y-1 max-h-80 overflow-y-auto pr-1 border border-gray-100 dark:border-gray-800 rounded">
          {nodes.map((n) => {
            const cfg = NODE_TYPE_CONFIG[n.node_type];
            return (
              <button
                key={n.id}
                type="button"
                onClick={() => setSelected(n)}
                className="w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-2"
              >
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: cfg?.color ?? '#6B7280' }}
                  aria-hidden
                />
                <span className="font-medium text-gray-800 dark:text-gray-200 truncate flex-1">
                  {n.title || n.id}
                </span>
                <span className="text-[10px] uppercase text-gray-400 shrink-0">
                  {n.node_type}
                </span>
                <span
                  className={
                    'text-[10px] shrink-0 ' +
                    (n.validation_status === 'unvalidated'
                      ? 'text-amber-600 dark:text-amber-400'
                      : 'text-green-700 dark:text-green-400')
                  }
                >
                  {n.validation_status}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {selected && (
        <NodeDetailModal
          boardId={boardId}
          node={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: number;
  tone?: 'ok' | 'warn' | 'neutral';
}) {
  const color =
    tone === 'ok'
      ? 'text-green-700 dark:text-green-400'
      : tone === 'warn'
        ? 'text-amber-700 dark:text-amber-400'
        : 'text-gray-900 dark:text-gray-100';
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}
