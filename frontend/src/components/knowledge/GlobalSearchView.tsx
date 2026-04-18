/**
 * GlobalSearchView — cross-board semantic search over the KG.
 * Searches all accessible boards via natural language query.
 */

import { useState } from 'react';
import * as kgApi from '@/services/kg-api';
import { NODE_TYPE_CONFIG, type KGNodeType } from '@/types/knowledge-graph';

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

export function GlobalSearchView({ boardId: _boardId }: Props) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

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

  return (
    <div className="p-6 max-w-3xl">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
        Global Discovery
      </h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
        Search across all accessible boards for decisions, constraints, and learnings using natural language.
      </p>

      <form onSubmit={handleSearch} className="flex gap-2 mb-6">
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
          <p className="text-sm">No results found. Try different keywords or ensure boards have consolidated KG data.</p>
        </div>
      )}

      {results.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-3">
            {results.length} result{results.length !== 1 ? 's' : ''} found
          </div>
          {results.map((r, i) => {
            const nt = (r.node_type ?? '') as KGNodeType;
            const cfg = NODE_TYPE_CONFIG[nt];
            return (
              <div
                key={`${r.board_id}-${r.id}-${i}`}
                className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 hover:bg-gray-50 dark:hover:bg-gray-800/50"
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
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
