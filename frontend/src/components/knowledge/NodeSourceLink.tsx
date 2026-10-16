import { useEffect, useState } from 'react';
import { ExternalLink, Loader2 } from 'lucide-react';
import type { KGNode } from '@/types/knowledge-graph';
import { getNodeSource, type KGNodeSource } from '@/services/kg-api';
import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { useDashboardStore } from '@/store/dashboard';

/** One selected node only: no enrichment of the graph's entire node page. */
export function NodeSourceLink({ node, boardId }: { node: KGNode; boardId?: string }) {
  // Reset synchronously when scope changes; old responses can never open a new board's entity.
  return <ScopedSource key={`${boardId}:${node.id}:${node.source_artifact_ref}`} node={node} boardId={boardId} />;
}

function ScopedSource({ node, boardId }: { node: KGNode; boardId?: string }) {
  const stack = useOptionalModalStack();
  const openCard = useDashboardStore((s) => s.openCardModal);
  const [result, setResult] = useState<KGNodeSource | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!boardId) return;
    const controller = new AbortController();
    setResult(null);
    setError(false);
    void (async () => {
      try {
        const value = await getNodeSource(boardId, node.id, controller.signal);
        if (!value) throw new Error('Invalid source response');
        if (!controller.signal.aborted) setResult(value);
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    })();
    return () => controller.abort();
  }, [boardId, node.id, attempt]);

  // A stale graph response may still name a retired entity. Preserve its source
  // reference below without offering an operational Sprint link.
  const target = result?.status === 'resolved' && result.target && result.target.board_id === boardId
    && ['spec', 'refinement', 'ideation', 'story', 'card'].includes(result.target.entity_type) ? result.target : null;
  const open = () => {
    if (!target || !stack) return;
    const top = stack.stack[stack.stack.length - 1];
    if (top?.type !== 'kg_node' || top.id !== node.id || top.boardId !== boardId) {
      stack.push({ type: 'kg_node', id: node.id, boardId });
    }
    if (target.entity_type === 'card') openCard(target.entity_id);
    stack.push({ type: target.entity_type, id: target.entity_id, boardId: target.board_id });
  };
  const message = !boardId ? 'A board context is required to resolve this source.'
    : result?.status === 'missing_source' ? 'No source was recorded for this node.'
    : result?.status === 'unsupported' ? 'This source has no supported owning entity. The original reference is preserved below.'
    : 'The source is unavailable or you do not have permission to view it.';

  return (
    <section className="my-3 rounded-lg border border-sky-200 bg-sky-50/60 p-3 text-xs dark:border-sky-900 dark:bg-sky-950/20" aria-label="Source entity" data-testid="kg-node-source">
      <h4 className="mb-2 font-semibold text-sky-800 dark:text-sky-300">Source entity</h4>
      {target ? (
        <>
          <p className="mb-1 capitalize text-gray-500 dark:text-gray-400">{target.entity_kind}</p>
          <p className="mb-2 break-words font-medium text-gray-900 dark:text-gray-100">{target.title}</p>
          <button type="button" onClick={open} disabled={!stack}
            className="inline-flex items-center gap-2 rounded bg-blue-600 px-3 py-1.5 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            aria-label={`Open ${target.entity_kind}: ${target.title}`}>
            <ExternalLink className="h-3.5 w-3.5" aria-hidden /> Open source
          </button>
          {target.source_version != null && <p className="mt-2 text-gray-500 dark:text-gray-400">Recorded against version {target.source_version}. Opens the current entity.</p>}
        </>
      ) : error ? (
        <div role="status" className="text-gray-600 dark:text-gray-300">Could not resolve the source. <button type="button" className="underline" onClick={() => setAttempt((n) => n + 1)}>Retry source lookup</button></div>
      ) : !result && boardId ? (
        <p role="status" className="flex items-center gap-2 text-gray-500 dark:text-gray-400"><Loader2 className="h-3 w-3 animate-spin" aria-hidden /> Resolving source…</p>
      ) : <p className="text-gray-600 dark:text-gray-300">{message}</p>}
      {(result?.source_artifact_ref || node.source_artifact_ref) && (
        <details className="mt-2 text-gray-500 dark:text-gray-400">
          <summary className="cursor-pointer">Original source reference</summary>
          <code className="mt-1 block break-all">{result?.source_artifact_ref || node.source_artifact_ref}</code>
        </details>
      )}
    </section>
  );
}
