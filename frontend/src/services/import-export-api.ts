/**
 * Import/Export API — schema_version-1 JSON envelopes for the three generic
 * catalog families: Design Systems, Permission Presets and Default Board
 * Configuration versions (ITEM 19). Guidelines use the governed lossless
 * guideline-export/v3 service instead of this generic schema-v1 surface.
 *
 * Envelope: { schema_version: "1", kind, exported_at, items: [...] }
 * Import response: { created, skipped: [...], errors: [...], dry_run } —
 * a 400 (invalid envelope / invalid item, nothing mutated) surfaces as a
 * thrown Error via the shared AuthenticatedFetch client.
 */

import { useApiClient } from '@/contexts/ApiContext';

export type ImportExportKind =
  | 'design_systems'
  | 'presets'
  | 'board_config';

export interface ImportExportEnvelope {
  schema_version: string;
  kind: ImportExportKind | string;
  exported_at?: string;
  items: Array<Record<string, unknown>>;
}

export interface ImportSummary {
  created: number;
  skipped: Array<Record<string, unknown>>;
  errors: Array<Record<string, unknown>>;
  dry_run?: boolean;
}

export interface ImportOptions {
  dryRun?: boolean;
}

/** guidelines-20260710.json */
export function importExportFilename(kind: string, date: Date = new Date()): string {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${kind}-${yyyy}${mm}${dd}.json`;
}

/** Trigger a browser download of `data` as a pretty-printed .json blob. */
export function downloadJsonFile(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function useImportExportApi() {
  const apiClient = useApiClient();

  const post = (path: string, envelope: ImportExportEnvelope, params: URLSearchParams) => {
    const qs = params.toString();
    return apiClient.fetchJson<ImportSummary>(`${path}${qs ? `?${qs}` : ''}`, {
      method: 'POST',
      body: JSON.stringify(envelope),
    });
  };

  return {
    // ==================== DESIGN SYSTEMS ====================

    async exportDesignSystems(): Promise<ImportExportEnvelope> {
      return apiClient.fetchJson<ImportExportEnvelope>('/design-systems/export');
    },

    async exportDesignSystem(designSystemId: string): Promise<ImportExportEnvelope> {
      return apiClient.fetchJson<ImportExportEnvelope>(
        `/design-systems/${designSystemId}/export`,
      );
    },

    async importDesignSystems(
      envelope: ImportExportEnvelope,
      options: ImportOptions = {},
    ): Promise<ImportSummary> {
      const params = new URLSearchParams();
      if (options.dryRun) params.set('dry_run', 'true');
      return post('/design-systems/import', envelope, params);
    },

    // ==================== PERMISSION PRESETS ====================

    async exportPresets(): Promise<ImportExportEnvelope> {
      return apiClient.fetchJson<ImportExportEnvelope>('/presets/export');
    },

    async importPresets(
      envelope: ImportExportEnvelope,
      options: ImportOptions = {},
    ): Promise<ImportSummary> {
      const params = new URLSearchParams();
      if (options.dryRun) params.set('dry_run', 'true');
      return post('/presets/import', envelope, params);
    },

    // ==================== DEFAULT BOARD CONFIG ====================

    async exportBoardConfig(scope = 'global'): Promise<ImportExportEnvelope> {
      const params = new URLSearchParams({ scope });
      return apiClient.fetchJson<ImportExportEnvelope>(
        `/default-board-config/export?${params.toString()}`,
      );
    },

    async importBoardConfig(
      envelope: ImportExportEnvelope,
      options: ImportOptions = {},
    ): Promise<ImportSummary> {
      const params = new URLSearchParams();
      if (options.dryRun) params.set('dry_run', 'true');
      return post('/default-board-config/import', envelope, params);
    },
  };
}
