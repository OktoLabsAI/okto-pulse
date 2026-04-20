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

import { useEffect, useRef, useState } from 'react';
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
  const [typeFilter, setTypeFilter] = useState<Set<string>>(new Set());
  const resultsRef = useRef<HTMLDivElement | null>(null);

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

  async function runSearch(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;
    setLoading(true);
    setSearched(true);
    setTypeFilter(new Set()); // reset filters on a new search
    try {
      const data = await kgApi.globalSearch(trimmed, 20);
      setResults(data.results || []);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
      // Scroll the results panel into view after the state settles.
      window.requestAnimationFrame(() => {
        resultsRef.current?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        });
      });
    }
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    await runSearch(query);
  }

  async function handleIntentClick(intent: DiscoveryIntent): Promise<void> {
    setActiveIntent(intent);
    // Seed the free-text box so the user can see / tweak what was asked, and
    // immediately fire the semantic search so the cards produce a real
    // result — not just informational text. The seed is the intent's
    // description when present (richer signal for embeddings) and falls
    // back to the label.
    const seed = intent.description?.trim() || intent.label;
    setQuery(seed);
    await runSearch(seed);
  }

  function clearSearch(): void {
    setQuery('');
    setResults([]);
    setSearched(false);
    setActiveIntent(null);
    setTypeFilter(new Set());
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
    <div className="p-6 flex flex-col h-full overflow-y-auto">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1">
        Global Discovery
      </h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
        Click a pre-built question to run it, or type your own in the
        free-text box below. Results from either path land in the same
        section at the bottom.
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
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-2.5">
                {intentsByCategory[cat].map((intent) => {
                  const isActive = activeIntent?.id === intent.id;
                  return (
                    <button
                      key={intent.id}
                      type="button"
                      onClick={() => handleIntentClick(intent)}
                      data-testid={`discovery-intent-${intent.name}`}
                      title="Run this intent"
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
                          <div className="mt-1.5 flex items-center gap-2 text-[10px]">
                            {intent.is_seed && (
                              <span className="px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                                built-in
                              </span>
                            )}
                            <span
                              className={`font-medium ${
                                isActive
                                  ? 'text-blue-600 dark:text-blue-300'
                                  : 'text-gray-500 dark:text-gray-500'
                              }`}
                            >
                              {isActive && loading
                                ? 'Running…'
                                : isActive
                                  ? 'Ran — see results below'
                                  : 'Click to run'}
                            </span>
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
            className="mt-3 text-xs text-gray-500 dark:text-gray-400"
            data-testid="discovery-intent-detail"
          >
            Running <span className="font-medium text-gray-700 dark:text-gray-200">{activeIntent.label}</span>{' '}
            — query seeded from the intent, executed against the KG semantic index. A richer result view (with
            the intent's bound tool called directly and saved searches) ships with the admin follow-up card.
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
          {(query || searched || activeIntent) && (
            <button
              type="button"
              onClick={clearSearch}
              data-testid="discovery-clear-inline"
              title="Clear query, filters and results"
              className="px-3 py-2 text-xs rounded-lg border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              Clear
            </button>
          )}
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

        {results.length > 0 && (() => {
          // Build the set of types present in the current result set, so the
          // filter chips are scoped to what the user actually got back.
          const typesInResults = Array.from(
            new Set(results.map((r) => r.node_type ?? 'Unknown')),
          ).sort();
          const filtered = typeFilter.size === 0
            ? results
            : results.filter((r) => typeFilter.has(r.node_type ?? 'Unknown'));
          const toggleType = (t: string) => {
            setTypeFilter((prev) => {
              const next = new Set(prev);
              if (next.has(t)) next.delete(t);
              else next.add(t);
              return next;
            });
          };
          const removeType = (t: string) => {
            setTypeFilter((prev) => {
              const next = new Set(prev);
              next.delete(t);
              return next;
            });
          };
          // Top N results → satellites around the query anchor on the mini-graph.
          const topSatellites = filtered.slice(0, 8);
          const centerLabel = activeIntent
            ? activeIntent.label.replace(/\?$/, '').slice(0, 28)
            : (query.trim().slice(0, 28) || 'Query');
          const activeFilterLabels = Array.from(typeFilter);
          const availableDimensions = ['status', 'sprint', 'assignee'].filter(
            (d) => !activeFilterLabels.includes(d),
          );
          const headingTitle = activeIntent
            ? activeIntent.label
            : `Search: "${query.trim()}"`;

          return (
            <div
              ref={resultsRef}
              className="flex-1"
              data-testid="global-search-results"
            >
              {/* Result-panel container matching mockup gd-02-result */}
              <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white/50 dark:bg-gray-900/40 p-5">
                {/* Header — intent title on left, Save/Export on the right */}
                <div className="flex items-start justify-between gap-3 mb-4">
                  <div className="min-w-0">
                    <div className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400">
                      {activeIntent ? 'Intent' : 'Query'}
                    </div>
                    <div className="text-base font-medium text-gray-900 dark:text-gray-100">
                      {headingTitle}
                    </div>
                    {activeIntent?.description && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-1">
                        {activeIntent.description}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <button
                      type="button"
                      onClick={clearSearch}
                      data-testid="discovery-clear"
                      title="Clear the current query, filters and results"
                      className="text-xs px-2.5 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1"
                    >
                      <span aria-hidden>✕</span> Clear
                    </button>
                    <button
                      type="button"
                      disabled
                      title="Saved searches ship with the admin follow-up card"
                      className="text-xs px-2.5 py-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 disabled:cursor-not-allowed"
                    >
                      Save search
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        const blob = new Blob(
                          [JSON.stringify(filtered, null, 2)],
                          { type: 'application/json' },
                        );
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `discovery-${Date.now()}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                      }}
                      className="text-xs px-2.5 py-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700"
                    >
                      Export
                    </button>
                  </div>
                </div>

                {/* Filter chips — active ones show "type: X ✕", inactives
                    show "+ dimension" as add-slots (matching mockup). */}
                <div
                  className="flex flex-wrap gap-2 mb-4 text-xs"
                  data-testid="discovery-result-filters"
                >
                  {typesInResults.map((t) => {
                    const cfg = NODE_TYPE_CONFIG[t as KGNodeType];
                    const active = typeFilter.has(t);
                    const count = results.filter(
                      (r) => (r.node_type ?? 'Unknown') === t,
                    ).length;
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() =>
                          active ? removeType(t) : toggleType(t)
                        }
                        aria-pressed={active}
                        className={`px-2 py-1 rounded-full transition-colors ${
                          active
                            ? 'text-white border-transparent'
                            : 'text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800'
                        }`}
                        style={
                          active && cfg
                            ? { backgroundColor: cfg.color }
                            : undefined
                        }
                      >
                        {active ? (
                          <>
                            type: {t} <span className="ml-1">✕</span>{' '}
                            <span className="opacity-80">({count})</span>
                          </>
                        ) : (
                          <>
                            type: {t}{' '}
                            <span className="opacity-70">({count})</span>
                          </>
                        )}
                      </button>
                    );
                  })}
                  {availableDimensions.map((d) => (
                    <button
                      key={d}
                      type="button"
                      disabled
                      title="Extra dimensions arrive when the admin UI lands"
                      className="px-2 py-1 rounded-full border border-dashed border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500 disabled:cursor-not-allowed"
                    >
                      + {d}
                    </button>
                  ))}
                </div>

                {/* Split content: table (7) + mini-graph + MCP (5) */}
                <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
                  <div className="xl:col-span-7 min-w-0">
                    <div className="mb-2 text-[11px] text-gray-500 dark:text-gray-400">
                      <strong className="text-gray-700 dark:text-gray-200">
                        {filtered.length}
                      </strong>{' '}
                      {filtered.length === 1 ? 'result' : 'results'}
                      {typeFilter.size > 0 && ` (of ${results.length})`}
                    </div>
                    <div className="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="bg-gray-50 dark:bg-gray-800/50 text-gray-500 dark:text-gray-400 uppercase tracking-wider text-[10px]">
                            <th className="text-left font-medium px-3 py-2">
                              Type
                            </th>
                            <th className="text-left font-medium px-3 py-2">
                              Title / Summary
                            </th>
                            <th className="text-right font-medium px-3 py-2">
                              Match
                            </th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                          {filtered.map((r, i) => {
                            const nt = (r.node_type ?? 'Unknown') as KGNodeType;
                            const cfg = NODE_TYPE_CONFIG[nt];
                            return (
                              <tr
                                key={`${r.board_id}-${r.id}-${i}`}
                                onClick={() => setSelected(r)}
                                data-testid={`global-search-result-${r.id}`}
                                className="cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50"
                              >
                                <td className="px-3 py-2 whitespace-nowrap align-top">
                                  {cfg ? (
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide text-white"
                                      style={{ backgroundColor: cfg.color }}
                                    >
                                      <span aria-hidden>{cfg.icon}</span>
                                      <span>{nt}</span>
                                    </span>
                                  ) : (
                                    <span className="text-gray-400">{nt}</span>
                                  )}
                                </td>
                                <td className="px-3 py-2 align-top">
                                  <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                                    {r.title || 'Untitled'}
                                  </div>
                                  {r.summary && (
                                    <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
                                      {r.summary}
                                    </div>
                                  )}
                                </td>
                                <td className="px-3 py-2 align-top whitespace-nowrap text-right text-xs text-gray-500 dark:text-gray-400">
                                  {Math.round(r.similarity * 100)}%
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  <div className="xl:col-span-5 flex flex-col min-w-0">
                    <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                      Mini-graph (top {topSatellites.length} of {filtered.length})
                    </div>
                    <div className="relative h-[260px] rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden bg-gray-50/60 dark:bg-gray-950/60">
                      {/* center anchor */}
                      <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col items-center z-10">
                        <div className="h-10 w-10 rounded-full bg-cyan-500/80 border-2 border-cyan-300 dark:border-cyan-400" />
                        <div className="text-[10px] mt-1 text-cyan-700 dark:text-cyan-200 text-center max-w-[140px] truncate">
                          {centerLabel}
                        </div>
                      </div>
                      {/* connecting lines */}
                      <svg
                        className="absolute inset-0 w-full h-full pointer-events-none"
                        aria-hidden
                      >
                        {topSatellites.map((_, i) => {
                          const n = Math.max(1, topSatellites.length);
                          const angle = (i / n) * Math.PI * 2 - Math.PI / 2;
                          const radiusX = 42; // % of container
                          const radiusY = 38;
                          const cx = 50 + radiusX * Math.cos(angle);
                          const cy = 50 + radiusY * Math.sin(angle);
                          return (
                            <line
                              key={i}
                              x1="50%"
                              y1="50%"
                              x2={`${cx}%`}
                              y2={`${cy}%`}
                              stroke="currentColor"
                              className="text-gray-300 dark:text-gray-700"
                              strokeWidth="1"
                              strokeDasharray="3 3"
                            />
                          );
                        })}
                      </svg>
                      {/* satellite nodes */}
                      {topSatellites.map((r, i) => {
                        const n = Math.max(1, topSatellites.length);
                        const angle = (i / n) * Math.PI * 2 - Math.PI / 2;
                        const radiusX = 42;
                        const radiusY = 38;
                        const left = 50 + radiusX * Math.cos(angle);
                        const top = 50 + radiusY * Math.sin(angle);
                        const nt = (r.node_type ?? 'Unknown') as KGNodeType;
                        const cfg = NODE_TYPE_CONFIG[nt];
                        return (
                          <button
                            key={`${r.board_id}-${r.id}-${i}`}
                            type="button"
                            onClick={() => setSelected(r)}
                            title={`${nt} — ${r.title}`}
                            className="absolute -translate-x-1/2 -translate-y-1/2 h-6 w-6 rounded-full border border-black/20 dark:border-white/20 hover:scale-110 transition-transform focus:outline-none focus:ring-2 focus:ring-blue-400"
                            style={{
                              left: `${left}%`,
                              top: `${top}%`,
                              backgroundColor: cfg?.color ?? '#6B7280',
                            }}
                          />
                        );
                      })}
                    </div>
                    <div className="mt-3 rounded-md bg-gray-100 dark:bg-gray-800/60 p-3 text-xs">
                      <div className="text-gray-500 dark:text-gray-400 mb-1">
                        MCP equivalent
                      </div>
                      <code className="text-blue-600 dark:text-cyan-300 font-mono text-[11px] break-all">
                        {activeIntent
                          ? `${activeIntent.tool_binding}(${
                              activeIntent.params_schema
                                ? Object.keys(activeIntent.params_schema)
                                    .map((k) => `${k}=…`)
                                    .join(', ')
                                : ''
                            })`
                          : `okto_pulse_kg_query_global(q="${query.trim().slice(0, 40)}${query.trim().length > 40 ? '…' : ''}")`}
                      </code>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          );
        })()}
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
