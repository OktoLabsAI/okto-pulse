/**
 * GlobalSearchView — user-facing Discovery screen.
 *
 * v2 layout (spec b4aa7560):
 *   - Top: grid of intent cards grouped by category, loaded from
 *     GET /api/v1/discovery/intents. Clicking a card surfaces the canned
 *     query (v1 shows the tool binding + params schema as guidance; actual
 *     execution lives on the result views those tools already drive).
 *   - Bottom: the existing free-text semantic search box, kept fully
 *     functional as the fallback when the user's question does not match
 *     an intent.
 *
 * Saved searches and history panels will land with the admin card; here
 * we just surface placeholder links to keep the v1 surface honest.
 */

import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import * as kgApi from '@/services/kg-api';
import * as discoveryApi from '@/services/discovery-api';
import { NODE_TYPE_CONFIG, type KGNodeType } from '@/types/knowledge-graph';
import type { DiscoveryIntent } from '@/types/discovery';
import { NodeDetailModal } from './NodeDetailModal';

interface Props {
  boardId: string;
}

interface SearchResult {
  board_id: string;
  id: string;
  digest_id?: string;
  title: string;
  summary?: string;
  node_type?: string;
  similarity: number;
}

const CATEGORY_LABELS: Record<string, string> = {
  coverage_tracing: 'Coverage & Tracing',
  decisions_history: 'Decisions & History',
  dependencies_blockers: 'Dependencies & Blockers',
  similarity_reuse: 'Similarity & Reuse',
};

function humanizeCategory(category: string): string {
  return (
    CATEGORY_LABELS[category] ??
    category
      .split('_')
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ')
  );
}

export function GlobalSearchView({ boardId: _boardId }: Props) {
  const [intents, setIntents] = useState<DiscoveryIntent[]>([]);
  const [loadingIntents, setLoadingIntents] = useState(true);
  const [activeIntent, setActiveIntent] = useState<DiscoveryIntent | null>(null);

  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [selected, setSelected] = useState<SearchResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoadingIntents(true);
      try {
        const data = await discoveryApi.listIntents();
        if (!cancelled) setIntents(data);
      } catch {
        if (!cancelled) setIntents([]);
      } finally {
        if (!cancelled) setLoadingIntents(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      const data = await kgApi.globalSearch(query.trim(), 20);
      setResults(data.results || []);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }

  // Group intents by category for display
  const intentsByCategory = intents.reduce<Record<string, DiscoveryIntent[]>>(
    (acc, it) => {
      const key = it.category || 'other';
      (acc[key] ??= []).push(it);
      return acc;
    },
    {},
  );
  const orderedCategories = Object.keys(intentsByCategory).sort();

  return (
    <div className="p-6 max-w-4xl flex flex-col h-full overflow-y-auto">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1">
        Global Discovery
      </h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
        Pick a pre-built question below — or type your own in the free-text
        box at the bottom to search the knowledge graph semantically.
      </p>

      {/* Intent cards grid */}
      <section className="mb-8" data-testid="discovery-intents">
        {loadingIntents ? (
          <div className="text-xs text-gray-500 dark:text-gray-500 py-4">
            Loading intents…
          </div>
        ) : intents.length === 0 ? (
          <div className="text-xs text-gray-500 dark:text-gray-500 py-4">
            No intents configured yet. Ask an admin to seed the catalog or use
            the free-text search below.
          </div>
        ) : (
          orderedCategories.map((cat) => (
            <div key={cat} className="mb-5">
              <div className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-2">
                {humanizeCategory(cat)}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
                {intentsByCategory[cat].map((intent) => {
                  const isActive = activeIntent?.id === intent.id;
                  return (
                    <button
                      key={intent.id}
                      type="button"
                      onClick={() => setActiveIntent(intent)}
                      data-testid={`discovery-intent-${intent.name}`}
                      className={`text-left rounded-lg border px-3 py-2.5 transition-colors ${
                        isActive
                          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                          : 'border-gray-200 dark:border-gray-700 hover:border-blue-400 dark:hover:border-blue-500/60 hover:bg-gray-50 dark:hover:bg-gray-800/50'
                      }`}
                    >
                      <div className="flex items-start gap-2">
                        <Sparkles
                          size={14}
                          className="mt-0.5 text-blue-500 shrink-0"
                        />
                        <div className="min-w-0">
                          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                            {intent.label}
                          </div>
                          {intent.description && (
                            <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
                              {intent.description}
                            </div>
                          )}
                          <div className="mt-1.5 text-[10px] font-mono text-gray-500 dark:text-gray-500">
                            {intent.tool_binding}
                            {intent.is_seed && (
                              <span className="ml-2 px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                                built-in
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))
        )}

        {activeIntent && (
          <div
            className="mt-4 p-3 rounded-lg border border-blue-300 dark:border-blue-500/40 bg-blue-50 dark:bg-blue-900/20 text-xs"
            data-testid="discovery-intent-detail"
          >
            <div className="font-medium text-gray-900 dark:text-gray-100 mb-1">
              {activeIntent.label}
            </div>
            <div className="text-gray-600 dark:text-gray-300 mb-2">
              {activeIntent.description ?? '—'}
            </div>
            <div className="font-mono text-[11px] text-gray-700 dark:text-gray-300">
              Binding: {activeIntent.tool_binding}
            </div>
            {activeIntent.params_schema && (
              <pre className="mt-1 text-[10px] font-mono bg-white/60 dark:bg-gray-900/40 rounded p-2 overflow-x-auto">
                {JSON.stringify(activeIntent.params_schema, null, 2)}
              </pre>
            )}
            <div className="mt-2 text-[10px] text-gray-500 dark:text-gray-400">
              Execution is performed by the bound tool. The dedicated result
              view (and saved searches) will land with the admin follow-up card.
            </div>
          </div>
        )}
      </section>

      {/* Free-text semantic search — unchanged from v1 */}
      <section className="border-t border-gray-200 dark:border-gray-700 pt-5">
        <div className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-2">
          Free semantic search
        </div>
        <form onSubmit={handleSearch} className="flex gap-2 mb-4 shrink-0">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g., authentication decisions, API rate limiting constraints..."
            className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          <button
            type="submit"
            disabled={loading || !query.trim()}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Searching...' : 'Search'}
          </button>
        </form>

        {searched && !loading && results.length === 0 && (
          <div className="text-center text-gray-500 dark:text-gray-400 py-8">
            <div className="text-3xl mb-2">🔍</div>
            <p className="text-sm">
              No results found. Try different keywords or ensure boards have
              consolidated KG data.
            </p>
          </div>
        )}

        {results.length > 0 && (
          <div
            className="space-y-2 flex-1 overflow-y-auto pr-1"
            data-testid="global-search-results"
          >
            <div className="text-xs text-gray-500 dark:text-gray-400 mb-3">
              {results.length} result{results.length !== 1 ? 's' : ''} found
            </div>
            {results.map((r, i) => {
              const nt = (r.node_type ?? '') as KGNodeType;
              const cfg = NODE_TYPE_CONFIG[nt];
              return (
                <button
                  key={`${r.board_id}-${r.id}-${i}`}
                  type="button"
                  onClick={() => setSelected(r)}
                  data-testid={`global-search-result-${r.id}`}
                  className="w-full text-left border border-gray-200 dark:border-gray-700 rounded-lg p-3 hover:bg-gray-50 dark:hover:bg-gray-800/50 focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        {cfg && (
                          <span
                            className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide text-white"
                            style={{ backgroundColor: cfg.color }}
                          >
                            <span>{cfg.icon}</span>
                            <span>{nt}</span>
                          </span>
                        )}
                        <div className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                          {r.title || 'Untitled'}
                        </div>
                      </div>
                      {r.summary && (
                        <div className="text-xs text-gray-600 dark:text-gray-300 mt-1.5 line-clamp-2">
                          {r.summary}
                        </div>
                      )}
                      <div className="text-[10px] text-gray-500 dark:text-gray-400 mt-1.5 font-mono">
                        Board: {r.board_id?.slice(0, 12)}... | Node: {r.id?.slice(0, 12)}...
                      </div>
                    </div>
                    <div className="text-xs text-gray-400 whitespace-nowrap">
                      {Math.round(r.similarity * 100)}% match
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </section>

      {selected && (
        <NodeDetailModal
          boardId={selected.board_id}
          nodeId={selected.id}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
