/**
 * SettingsView — displays and manages KG settings for a board.
 * Shows provider info, consolidation status, and configuration.
 */

import { useState, useEffect } from 'react';
import toast from 'react-hot-toast';
import * as kgApi from '@/services/kg-api';
import { KGRefreshButton } from './KGRefreshButton';

interface Props {
  boardId: string;
}

interface BoardKGSettings {
  consolidation_enabled: boolean;
  enable_historical_consolidation: boolean;
  kg_initialized: boolean;
  embedding_provider: string | null;
  graph_store: string | null;
  session_ttl_seconds: number;
  kg_base_dir: string | null;
  // Enrichment fields from /kg/settings (FR-6). Optional for backwards compat
  // with older backends that predate Spec 3.
  embedding_provider_name?: string;
  model_name?: string | null;
  embedding_dimension?: number;
  is_loaded?: boolean;
  is_stub?: boolean;
}

export function SettingsView({ boardId }: Props) {
  const [settings, setSettings] = useState<BoardKGSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadSettings();
  }, [boardId]);

  async function loadSettings() {
    setLoading(true);
    setError(null);
    try {
      const data = await kgApi.getKGSettings(boardId) as unknown as BoardKGSettings;
      setSettings(data);
    } catch (err: any) {
      setError(err.message || 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center">
        <div className="animate-pulse text-gray-400">Loading settings...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <p className="text-red-500 mb-3">{error}</p>
        <button onClick={loadSettings} className="text-sm text-blue-600 hover:underline">
          Retry
        </button>
      </div>
    );
  }

  if (!settings) return null;

  const providerName = settings.embedding_provider_name || settings.embedding_provider || 'Unknown';
  const modelName = settings.model_name ?? null;
  const embeddingDimension = settings.embedding_dimension ?? 0;
  const isLoaded = settings.is_loaded ?? !!settings.embedding_provider;
  // Older backends without the enrichment fields can't tell us whether they are
  // in stub mode. Fall back to a case-insensitive name check so the banner
  // still renders correctly against a pre-Spec-3 server.
  const isStub =
    settings.is_stub ?? /stub/i.test(settings.embedding_provider || providerName);

  return (
    <div className="p-6 max-w-2xl">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Knowledge Graph Settings
        </h2>
        <KGRefreshButton
          onRefresh={loadSettings}
          loading={loading}
          testId="settings-refresh"
        />
      </div>

      {isStub && (
        <div
          role="alert"
          className="mb-6 flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-100"
        >
          <span aria-hidden className="mt-0.5 text-lg leading-none">!</span>
          <div className="flex-1 text-sm">
            <p className="font-medium">Running in stub mode — semantic search disabled</p>
            <p className="mt-1 text-amber-800/90 dark:text-amber-200/90">
              The Knowledge Graph is using a deterministic hash-based embedder,
              so find_similar and query_global return hash coincidences instead
              of real semantic proximity.
            </p>
            <a
              href="https://github.com/okto-labs/okto-pulse#embedding-providers"
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block font-medium text-amber-900 underline hover:text-amber-950 dark:text-amber-100 dark:hover:text-amber-50"
            >
              How to enable
            </a>
          </div>
        </div>
      )}

      {/* Status overview */}
      <div className="grid grid-cols-2 gap-4 mb-8">
        <StatusCard
          label="KG Status"
          value={settings.kg_initialized ? 'Initialized' : 'Not initialized'}
          ok={settings.kg_initialized}
        />
        <StatusCard
          label="Consolidation"
          value={settings.consolidation_enabled ? 'Enabled' : 'Disabled'}
          ok={settings.consolidation_enabled}
        />
        <StatusCard
          label="Graph Store"
          value={settings.graph_store || 'Not configured'}
          ok={!!settings.graph_store}
        />
        <StatusCard
          label="Embedding Provider"
          value={providerName}
          ok={isLoaded && !isStub}
        />
      </div>

      {/* Configuration details */}
      <div className="space-y-4">
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 uppercase tracking-wider">
          Configuration
        </h3>

        <ConfigRow
          label="Embedding model"
          value={modelName || (isStub ? 'hash-based stub (no model)' : 'Not reported')}
        />
        <ConfigRow
          label="Embedding dimension"
          value={embeddingDimension ? String(embeddingDimension) : 'Unknown'}
        />
        <ConfigRow
          label="Model loaded"
          value={isLoaded ? 'Yes' : 'No'}
        />
        <ConfigRow label="Session TTL" value={`${settings.session_ttl_seconds}s`} />
        <ConfigRow label="Data directory" value={settings.kg_base_dir || '~/.okto-pulse'} />
        <ConfigRow
          label="Historical consolidation"
          value={settings.enable_historical_consolidation ? 'Active' : 'Inactive'}
        />
      </div>

      {/* Danger zone */}
      <div className="mt-10 pt-6 border-t border-red-200 dark:border-red-900/30">
        <h3 className="text-sm font-medium text-red-600 dark:text-red-400 uppercase tracking-wider mb-3">
          Danger Zone
        </h3>
        <button
          onClick={async () => {
            if (!confirm('This will permanently delete all KG data for this board. Continue?')) return;
            try {
              await kgApi.deleteKG(boardId);
              toast.success('Knowledge graph data deleted');
              loadSettings();
            } catch (err: any) {
              toast.error(err.message || 'Failed to delete KG data');
            }
          }}
          className="px-4 py-2 text-sm border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20"
        >
          Delete KG Data
        </button>
      </div>
    </div>
  );
}

function StatusCard({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
      <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-gray-400'}`} />
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{value}</span>
      </div>
    </div>
  );
}

function ConfigRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between py-2 border-b border-gray-100 dark:border-gray-800">
      <span className="text-sm text-gray-600 dark:text-gray-400">{label}</span>
      <span className="text-sm font-mono text-gray-900 dark:text-gray-100">{value}</span>
    </div>
  );
}
