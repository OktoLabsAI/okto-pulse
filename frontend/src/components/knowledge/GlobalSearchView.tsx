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
import { ChevronDown, ChevronRight, Sparkles } from 'lucide-react';
import * as kgApi from '@/services/kg-api';
import * as discoveryApi from '@/services/discovery-api';
import type { IntentExecutionResult } from '@/services/discovery-api';
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

export function GlobalSearchView({ boardId }: Props) {
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

  // Real-tool execution state (ideação a4f526df).
  const [intentResult, setIntentResult] = useState<IntentExecutionResult | null>(null);
  const [pendingIntent, setPendingIntent] = useState<DiscoveryIntent | null>(null);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [intentError, setIntentError] = useState<string | null>(null);
  const [intentsOpen, setIntentsOpen] = useState<boolean>(() => {
    if (typeof window === 'undefined') return true;
    return window.localStorage.getItem('discovery-intents-open') !== '0';
  });
  const toggleIntents = () => {
    setIntentsOpen((prev) => {
      const next = !prev;
      window.localStorage.setItem('discovery-intents-open', next ? '1' : '0');
      return next;
    });
  };

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
    // Free-text search clears any prior intent execution so the header
    // doesn't misrepresent the result panel (ideação 2356e620).
    setIntentResult(null);
    setActiveIntent(null);
    setIntentError(null);
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

  async function runIntent(
    intent: DiscoveryIntent,
    params: Record<string, string>,
  ): Promise<void> {
    setActiveIntent(intent);
    setIntentError(null);
    setLoading(true);
    setSearched(true);
    setResults([]); // Clear any prior semantic-search results.
    setIntentResult(null);
    try {
      const data = await discoveryApi.executeIntent(intent.id, boardId, params);
      setIntentResult(data);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to run intent';
      setIntentError(msg);
    } finally {
      setLoading(false);
      setPendingIntent(null);
      setParamValues({});
      window.requestAnimationFrame(() => {
        resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }

  async function handleIntentClick(intent: DiscoveryIntent): Promise<void> {
    const schema = intent.params_schema || {};
    const requiredKeys = Object.entries(schema)
      .filter(([, meta]) => (meta as { required?: boolean }).required)
      .map(([k]) => k);
    if (requiredKeys.length === 0) {
      await runIntent(intent, {});
      return;
    }
    // Open the inline params form; execution happens when the user confirms.
    setPendingIntent(intent);
    setParamValues(
      Object.fromEntries(Object.keys(schema).map((k) => [k, ''])),
    );
    setActiveIntent(null);
    setIntentResult(null);
  }

  function clearSearch(): void {
    setQuery('');
    setResults([]);
    setSearched(false);
    setActiveIntent(null);
    setTypeFilter(new Set());
    setIntentResult(null);
    setIntentError(null);
    setPendingIntent(null);
    setParamValues({});
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
      <div className="flex items-baseline justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Global Discovery
        </h2>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Type a semantic query above or expand the catalog below for
          pre-built questions.
        </p>
      </div>

      {/* 1. Semantic search bar at the top */}
      <section className="mb-4">
        <form onSubmit={handleSearch} className="flex gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g., authentication decisions, API rate limiting constraints..."
            className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            data-testid="discovery-search-input"
          />
          <button
            type="submit"
            disabled={loading || !query.trim()}
            data-testid="discovery-search-submit"
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
          <button
            type="button"
            onClick={clearSearch}
            disabled={!query && !searched && !activeIntent}
            data-testid="discovery-clear-inline"
            title="Clear query, filters and results"
            className="px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Clear
          </button>
        </form>
      </section>

      {/* 2. Collapsible intent catalog */}
      <section className="mb-6" data-testid="discovery-intents">
        <button
          type="button"
          onClick={toggleIntents}
          aria-expanded={intentsOpen}
          aria-controls="discovery-intents-panel"
          data-testid="discovery-intents-toggle"
          className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-800 text-sm transition-colors"
        >
          <span className="flex items-center gap-2 text-gray-800 dark:text-gray-200">
            {intentsOpen ? (
              <ChevronDown size={16} className="text-gray-500" />
            ) : (
              <ChevronRight size={16} className="text-gray-500" />
            )}
            <span className="font-medium">Pre-built questions</span>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              ({intents.length} across {orderedCategories.length} categor
              {orderedCategories.length === 1 ? 'y' : 'ies'})
            </span>
          </span>
          <span className="text-[11px] text-gray-500 dark:text-gray-400">
            {intentsOpen ? 'Click to collapse' : 'Click to expand'}
          </span>
        </button>

        {intentsOpen && (
          <div
            id="discovery-intents-panel"
            className="mt-3 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
          >
            {loadingIntents ? (
              <div className="text-xs text-gray-500 dark:text-gray-500 py-2">
                Loading intents…
              </div>
            ) : intents.length === 0 ? (
              <div className="text-xs text-gray-500 dark:text-gray-500 py-2">
                No intents configured yet. Ask an admin to seed the catalog
                or use the free-text search above.
              </div>
            ) : (
              orderedCategories.map((cat, idx) => (
                <div
                  key={cat}
                  className={idx === orderedCategories.length - 1 ? '' : 'mb-5'}
                >
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
          </div>
        )}
      </section>

      {/* Params form for intents that declare required params (ideação
          643eae49). Rendered inline between the catalog and the results. */}
      {pendingIntent && (
        <section
          className="mb-6 rounded-lg border border-blue-300 dark:border-blue-500/60 bg-blue-50 dark:bg-blue-900/20 p-4"
          data-testid="discovery-params-form"
        >
          <div className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">
            {pendingIntent.label}
          </div>
          <div className="text-xs text-gray-600 dark:text-gray-300 mb-3">
            This intent needs input before it can run. Fill the required
            fields below then press Run.
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runIntent(pendingIntent, paramValues);
            }}
            className="space-y-2"
          >
            {Object.entries(pendingIntent.params_schema || {}).map(
              ([key, meta]) => {
                const m = meta as {
                  required?: boolean;
                  label?: string;
                  type?: string;
                };
                return (
                  <div key={key} className="flex items-center gap-2">
                    <label className="text-xs text-gray-700 dark:text-gray-200 w-28 shrink-0">
                      {m.label || key}
                      {m.required && <span className="text-red-500">*</span>}
                    </label>
                    <input
                      type="text"
                      value={paramValues[key] ?? ''}
                      onChange={(e) =>
                        setParamValues((prev) => ({
                          ...prev,
                          [key]: e.target.value,
                        }))
                      }
                      data-testid={`discovery-param-${key}`}
                      className="flex-1 px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                      placeholder={m.label || key}
                    />
                  </div>
                );
              },
            )}
            <div className="flex gap-2 pt-2">
              <button
                type="submit"
                data-testid="discovery-params-run"
                disabled={loading}
                className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {loading ? 'Running…' : 'Run'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setPendingIntent(null);
                  setParamValues({});
                }}
                className="px-3 py-1.5 text-sm rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800"
              >
                Cancel
              </button>
            </div>
          </form>
        </section>
      )}

      {/* 3. Results area */}
      <section>
        {!searched && !loading && !pendingIntent && (
          <div className="text-xs text-gray-500 dark:text-gray-400 py-8 text-center border border-dashed border-gray-300 dark:border-gray-700 rounded-lg">
            Run a search above or pick a pre-built question to see results
            here.
          </div>
        )}

        {intentError && !loading && (
          <div
            className="mb-3 rounded border border-red-300 dark:border-red-500/60 bg-red-50 dark:bg-red-900/20 px-3 py-2 text-xs text-red-700 dark:text-red-300"
            data-testid="discovery-intent-error"
          >
            {intentError}
          </div>
        )}

        {/* Intent execution panel (ideação a4f526df + 2356e620).
            Shown whenever we have a real-tool payload back from the server. */}
        {intentResult && !loading && (
          <div
            ref={resultsRef}
            data-testid="discovery-intent-result"
            className="mb-6 rounded-lg border border-gray-200 dark:border-gray-700 bg-white/50 dark:bg-gray-900/40 p-5"
          >
            <div className="flex items-start justify-between gap-3 mb-3">
              <div className="min-w-0">
                <div className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Intent · real-tool execution
                </div>
                <div className="text-base font-medium text-gray-900 dark:text-gray-100">
                  {activeIntent?.label || intentResult.intent_name}
                </div>
                {activeIntent?.description && (
                  <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-1">
                    {activeIntent.description}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={clearSearch}
                data-testid="discovery-clear-intent"
                className="text-xs px-2.5 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1 shrink-0"
              >
                <span aria-hidden>✕</span> Clear
              </button>
            </div>
            <div className="mb-3 rounded-md bg-gray-100 dark:bg-gray-800/60 p-3 text-xs">
              <div className="text-gray-500 dark:text-gray-400 mb-1">
                Tool executed
              </div>
              <code className="text-blue-600 dark:text-cyan-300 font-mono text-[11px] break-all">
                {intentResult.tool_binding}(
                {Object.entries(intentResult.params_echo)
                  .map(([k, v]) => `${k}="${String(v).slice(0, 40)}"`)
                  .join(', ')}
                )
              </code>
            </div>
            {intentResult.rows.length === 0 ? (
              <div className="text-center text-gray-500 dark:text-gray-400 py-6">
                <div className="text-3xl mb-2">📭</div>
                <p className="text-sm">
                  The tool ran successfully but returned no rows for this
                  board.
                </p>
              </div>
            ) : (
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
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {intentResult.rows.map((r, i) => (
                      <tr
                        key={`${r.id}-${i}`}
                        data-testid={`discovery-intent-row-${i}`}
                        className="hover:bg-gray-50 dark:hover:bg-gray-800/50"
                      >
                        <td className="px-3 py-2 whitespace-nowrap align-top">
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide text-white bg-indigo-500">
                            {r.type}
                          </span>
                        </td>
                        <td className="px-3 py-2 align-top">
                          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                            {r.title || 'Untitled'}
                          </div>
                          {r.summary && (
                            <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                              {r.summary}
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mt-3 text-[11px] text-gray-500 dark:text-gray-400">
              <strong>{intentResult.total}</strong>{' '}
              {intentResult.total === 1 ? 'row' : 'rows'} from{' '}
              <code className="font-mono">{intentResult.tool_binding}</code>
            </div>
          </div>
        )}

        {searched && !loading && !intentResult && results.length === 0 && !intentError && (
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
