/**
 * KGHelpModal — In-app help modal explaining how Knowledge Graph works.
 */

import { useEffect } from 'react';
import { X, BookOpen, GitBranch, Database, Zap, Users, Eye } from 'lucide-react';

interface KGHelpModalProps {
  onClose: () => void;
}

export function KGHelpModal({ onClose }: KGHelpModalProps) {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="relative w-[90vw] max-w-3xl bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden"
        style={{ height: '80vh' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2">
            <BookOpen size={20} className="text-violet-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Knowledge Graph</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-6 space-y-6 text-left">
          {/* What is KG */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <Database size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">What is Knowledge Graph?</h3>
            </div>
            <p className="text-sm text-gray-600 dark:text-gray-400 leading-relaxed pl-7">
              The Knowledge Graph extracts <strong>decisions, constraints, learnings, and relationships</strong> from your specs, cards, and sprints
              into a searchable, interactive graph powered by <strong>Kuzu</strong> (embedded graph database). It enables complex queries,
              contradiction detection, supersedence tracking, and semantic similarity search.
            </p>
          </section>

          {/* How it works */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <GitBranch size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">How It Works</h3>
            </div>
            <div className="space-y-3 pl-7">
              <div className="flex items-start gap-3">
                <div className="flex-shrink-0 w-6 h-6 rounded-full bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400 flex items-center justify-center text-xs font-bold">1</div>
                <div className="text-left">
                  <h4 className="text-sm font-medium text-gray-900 dark:text-white">Continuous Consolidation</h4>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                    Each time a card or sprint is completed, the system automatically extracts entities and relationships into the graph.
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <div className="flex-shrink-0 w-6 h-6 rounded-full bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400 flex items-center justify-center text-xs font-bold">2</div>
                <div className="text-left">
                  <h4 className="text-sm font-medium text-gray-900 dark:text-white">Historical Consolidation</h4>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                    Processes existing done specs and closed sprints retroactively. Enable in the <strong>Settings</strong> sub-view.
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <div className="flex-shrink-0 w-6 h-6 rounded-full bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400 flex items-center justify-center text-xs font-bold">3</div>
                <div className="text-left">
                  <h4 className="text-sm font-medium text-gray-900 dark:text-white">Interactive Visualization</h4>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                    Navigate the graph with React Flow — pan, zoom, filter by type/confidence, click nodes for details, and explore relationships.
                  </p>
                </div>
              </div>
            </div>
          </section>

          {/* Node Types */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <Zap size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">Node Types (11)</h3>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs pl-7">
              <div className="p-2 bg-blue-50 dark:bg-blue-900/20 rounded border border-blue-200 dark:border-blue-800">
                <span className="font-medium text-blue-700 dark:text-blue-300">⚖️ Decision</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Architecture & product decisions</p>
              </div>
              <div className="p-2 bg-green-50 dark:bg-green-900/20 rounded border border-green-200 dark:border-green-800">
                <span className="font-medium text-green-700 dark:text-green-300">✓ Criterion</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Acceptance criteria</p>
              </div>
              <div className="p-2 bg-red-50 dark:bg-red-900/20 rounded border border-red-200 dark:border-red-800">
                <span className="font-medium text-red-700 dark:text-red-300">🚫 Constraint</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Technical/business limits</p>
              </div>
              <div className="p-2 bg-amber-50 dark:bg-amber-900/20 rounded border border-amber-200 dark:border-amber-800">
                <span className="font-medium text-amber-700 dark:text-amber-300">❓ Assumption</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Unvalidated premises</p>
              </div>
              <div className="p-2 bg-purple-50 dark:bg-purple-900/20 rounded border border-purple-200 dark:border-purple-800">
                <span className="font-medium text-purple-700 dark:text-purple-300">📋 Requirement</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Functional/technical reqs</p>
              </div>
              <div className="p-2 bg-cyan-50 dark:bg-cyan-900/20 rounded border border-cyan-200 dark:border-cyan-800">
                <span className="font-medium text-cyan-700 dark:text-cyan-300">🏷️ Entity</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Domain concepts</p>
              </div>
              <div className="p-2 bg-pink-50 dark:bg-pink-900/20 rounded border border-pink-200 dark:border-pink-800">
                <span className="font-medium text-pink-700 dark:text-pink-300">📡 APIContract</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Endpoint definitions</p>
              </div>
              <div className="p-2 bg-teal-50 dark:bg-teal-900/20 rounded border border-teal-200 dark:border-teal-800">
                <span className="font-medium text-teal-700 dark:text-teal-300">🧪 TestScenario</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Test cases</p>
              </div>
              <div className="p-2 bg-red-50 dark:bg-red-900/20 rounded border border-red-200 dark:border-red-800">
                <span className="font-medium text-red-700 dark:text-red-300">🐛 Bug</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Defects tracked</p>
              </div>
              <div className="p-2 bg-violet-50 dark:bg-violet-900/20 rounded border border-violet-200 dark:border-violet-800">
                <span className="font-medium text-violet-700 dark:text-violet-300">💡 Learning</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Lessons from incidents</p>
              </div>
              <div className="p-2 bg-gray-50 dark:bg-gray-800/40 rounded border border-gray-200 dark:border-gray-700">
                <span className="font-medium text-gray-700 dark:text-gray-300">↔️ Alternative</span>
                <p className="text-gray-600 dark:text-gray-400 mt-0.5">Options not chosen</p>
              </div>
            </div>
          </section>

          {/* Edge Types */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <GitBranch size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">Relationship Types (10)</h3>
            </div>
            <div className="flex flex-wrap gap-1.5 pl-7">
              {['supersedes', 'contradicts', 'derives_from', 'relates_to', 'mentions', 'depends_on', 'violates', 'implements', 'tests', 'validates'].map(t => (
                <span key={t} className={`px-2 py-0.5 rounded text-xs font-mono ${
                  t === 'contradicts' ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300' :
                  t === 'supersedes' ? 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300' :
                  'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300'
                }`}>
                  {t}
                </span>
              ))}
            </div>
          </section>

          {/* Sub-views */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <Eye size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">Sub-views</h3>
            </div>
            <div className="space-y-2 pl-7 text-xs">
              <div className="flex items-start gap-2">
                <span className="font-medium text-gray-900 dark:text-white w-24 shrink-0">Graph</span>
                <span className="text-gray-600 dark:text-gray-400">Interactive visualization — click nodes for details, use Find Similar and Show History</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="font-medium text-gray-900 dark:text-white w-24 shrink-0">Audit Log</span>
                <span className="text-gray-600 dark:text-gray-400">Consolidation history — nodes added/updated/superseded, edges created, agent tracking</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="font-medium text-gray-900 dark:text-white w-24 shrink-0">Pending</span>
                <span className="text-gray-600 dark:text-gray-400">Queue of artifacts waiting to be consolidated into the graph</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="font-medium text-gray-900 dark:text-white w-24 shrink-0">Settings</span>
                <span className="text-gray-600 dark:text-gray-400">Provider status, enable historical consolidation, data directory, danger zone</span>
              </div>
              <div className="flex items-start gap-2">
                <span className="font-medium text-gray-900 dark:text-white w-24 shrink-0">Global Search</span>
                <span className="text-gray-600 dark:text-gray-400">Cross-board semantic search — find decisions and context by natural language query</span>
              </div>
            </div>
          </section>

          {/* AI Agent Integration */}
          <section>
            <div className="flex items-center gap-2 mb-3">
              <Users size={18} className="text-violet-500 flex-shrink-0" />
              <h3 className="text-md font-semibold text-gray-900 dark:text-white">AI Agent Integration</h3>
            </div>
            <p className="text-sm text-gray-600 dark:text-gray-400 leading-relaxed pl-7 mb-2">
              MCP agents have 15+ KG tools available, including:
            </p>
            <div className="grid grid-cols-2 gap-1.5 pl-7 text-xs">
              {[
                ['kg_get_decision_history', 'Past decisions on a topic'],
                ['kg_find_contradictions', 'Conflicting decisions'],
                ['kg_find_similar_decisions', 'Semantic similarity'],
                ['kg_explain_constraint', 'Trace constraint source'],
                ['kg_get_supersedence_chain', 'What replaced what'],
                ['kg_query_cypher', 'Direct Cypher (read-only)'],
              ].map(([tool, desc]) => (
                <div key={tool} className="flex items-start gap-1">
                  <code className="text-violet-600 dark:text-violet-400 font-mono text-[10px] shrink-0">{tool}</code>
                  <span className="text-gray-500 dark:text-gray-500">— {desc}</span>
                </div>
              ))}
            </div>
          </section>

          {/* Getting Started */}
          <section className="bg-violet-50 dark:bg-violet-900/20 rounded-lg p-4 border border-violet-200 dark:border-violet-800">
            <h4 className="text-sm font-semibold text-violet-900 dark:text-violet-100 mb-2">Getting Started</h4>
            <ol className="text-xs text-violet-800 dark:text-violet-200 space-y-1 list-decimal list-inside">
              <li>Go to the <strong>Settings</strong> sub-view and click <strong>"Enable Historical Consolidation"</strong></li>
              <li>Monitor progress in the <strong>Pending</strong> sub-view</li>
              <li>Switch to the <strong>Graph</strong> view to explore extracted entities</li>
              <li>Use the <strong>type filter</strong> and <strong>confidence slider</strong> to focus on what matters</li>
              <li>Click any node to see details, find similar decisions, or trace supersedence chains</li>
              <li>Try <strong>Global Search</strong> for cross-board discovery by natural language</li>
            </ol>
          </section>
        </div>
      </div>
    </div>
  );
}
