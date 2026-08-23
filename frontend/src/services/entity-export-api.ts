import { useMemo } from 'react';

import { useApiClient } from '@/contexts/ApiContext';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import {
  assertPassiveStandaloneHtml,
  contentDispositionFilename,
  safeDownloadFilename,
} from '@/lib/entity-export/security';
import type {
  EntityExportDownload,
  EntityExportDownloadRequest,
  EntityExportFormat,
  EntityExportIdentity,
  EntityExportManifestSection,
  EntityExportPreflight,
  EntityExportPreflightRequest,
  EntityExportScope,
  EntityExportSectionState,
  EntityExportType,
} from '@/types/entity-export';

type JsonRecord = Record<string, unknown>;

const SECTION_STATES = new Set<EntityExportSectionState>([
  'included',
  'empty',
  'omitted',
  'unavailable',
  'not_applicable',
  'error',
]);

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord
    : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function boolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function integer(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.trunc(value)
    : null;
}

function parseFormats(value: unknown): EntityExportFormat[] {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.filter(
    (item): item is EntityExportFormat => item === 'markdown' || item === 'html',
  )));
}

function parseIdentity(
  value: unknown,
  expectedType: EntityExportType,
  expectedId: string,
): EntityExportIdentity {
  const raw = record(value);
  if (!raw) throw new Error('Export preflight did not include entity identity.');
  const entityType = text(raw.entity_type ?? raw.type);
  const entityId = text(raw.entity_id ?? raw.id);
  const title = text(raw.title ?? raw.name);
  if (entityType !== expectedType || entityId !== expectedId || !title) {
    throw new Error('Export preflight identity does not match the requested entity.');
  }
  return {
    entity_type: expectedType,
    entity_id: expectedId,
    title,
    status: text(raw.status),
    edition: integer(raw.edition),
    version: integer(raw.version),
  };
}

function humanizeSectionKey(key: string): string {
  return key
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(' ');
}

function parseSection(value: unknown, fallbackKey?: string): EntityExportManifestSection {
  const raw = record(value);
  if (!raw) throw new Error('Export preflight contains an invalid manifest section.');
  const sectionKey = text(raw.section_key ?? raw.key ?? raw.id) ?? fallbackKey ?? null;
  const rawState = text(raw.state ?? raw.status);
  if (!sectionKey || !rawState || !SECTION_STATES.has(rawState as EntityExportSectionState)) {
    throw new Error('Export preflight contains an unrecognized manifest section state.');
  }
  return {
    section_key: sectionKey,
    label: text(raw.label ?? raw.title) ?? humanizeSectionKey(sectionKey),
    state: rawState as EntityExportSectionState,
    reason_code: text(raw.reason_code ?? raw.reason),
    message: text(raw.message ?? raw.detail),
    total_count: integer(raw.total_count ?? raw.count),
  };
}

function parseSections(value: unknown): EntityExportManifestSection[] {
  if (Array.isArray(value)) return value.map((item) => parseSection(item));
  const raw = record(value);
  if (!raw) return [];
  if (Array.isArray(raw.entries)) return raw.entries.map((item) => parseSection(item));
  const nestedSections = record(raw.sections);
  if (nestedSections) {
    return Object.entries(nestedSections).map(([key, item]) => parseSection(item, key));
  }
  return Object.entries(raw).map(([key, item]) => parseSection(item, key));
}

export function parseEntityExportPreflight(
  value: unknown,
  expectedType: EntityExportType,
  expectedId: string,
  expectedScope: EntityExportScope,
): EntityExportPreflight {
  const raw = record(value);
  if (!raw) throw new Error('Export preflight response is not an object.');
  const scope = text(raw.scope ?? raw.history_scope);
  if (scope !== expectedScope) {
    throw new Error('Export preflight scope does not match the request.');
  }
  const snapshot = record(raw.snapshot);
  const fingerprint = text(
    raw.snapshot_fingerprint
      ?? raw.fingerprint
      ?? snapshot?.fingerprint
      ?? snapshot?.snapshot_fingerprint,
  );
  if (!fingerprint || !/^[0-9a-f]{64}$/.test(fingerprint)) {
    throw new Error('Export preflight contains an invalid snapshot fingerprint.');
  }
  const manifest = record(raw.manifest);
  const sections = parseSections(manifest?.entries ?? raw.sections ?? raw.manifest);
  if (sections.length === 0) throw new Error('Export preflight returned an empty manifest.');
  const formats = parseFormats(raw.formats ?? raw.supported_formats);
  if (formats.length === 0) throw new Error('Export preflight returned no supported formats.');
  const completeForActor = boolean(raw.complete_for_actor ?? manifest?.complete_for_actor);
  const sourceComplete = boolean(raw.source_complete ?? manifest?.source_complete);
  if (completeForActor === null || sourceComplete === null) {
    throw new Error('Export preflight is missing completeness authority.');
  }
  return {
    schema_version: text(
      raw.schema_version
        ?? raw.contract_version
        ?? manifest?.contract_version,
    ) ?? 'entity-export/v1',
    formats,
    scope: expectedScope,
    identity: parseIdentity(raw.identity ?? raw.entity ?? raw.subject, expectedType, expectedId),
    snapshot_fingerprint: fingerprint,
    sections,
    complete_for_actor: completeForActor,
    source_complete: sourceComplete,
  };
}

async function responseError(response: Response): Promise<AuthenticatedFetchError> {
  const body = await response.json().catch(() => null) as JsonRecord | null;
  const detail = record(body?.detail) ?? record(body?.backend_error);
  const message = text(detail?.message ?? body?.message ?? body?.error)
    ?? `Export request failed (${response.status}).`;
  return new AuthenticatedFetchError({
    message,
    status: response.status,
    code: text(detail?.code ?? body?.code),
    details: detail?.details ?? body?.details ?? detail,
    retryable: boolean(detail?.retryable ?? body?.retryable) ?? false,
  });
}

export function useEntityExportApi() {
  const apiClient = useApiClient();
  return useMemo(() => ({
    async preflight(
      boardId: string,
      entityType: EntityExportType,
      entityId: string,
      request: EntityExportPreflightRequest,
      signal?: AbortSignal,
    ): Promise<EntityExportPreflight> {
      const raw = await apiClient.fetchJson<unknown>(
        `/boards/${encodeURIComponent(boardId)}/entity-exports/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/preflight`,
        {
          method: 'POST',
          body: JSON.stringify(request),
          signal,
        },
      );
      return parseEntityExportPreflight(raw, entityType, entityId, request.scope);
    },

    async download(
      boardId: string,
      entityType: EntityExportType,
      entityId: string,
      request: EntityExportDownloadRequest,
      fallbackTitle: string,
      signal?: AbortSignal,
    ): Promise<EntityExportDownload> {
      const response = await apiClient.fetch(
        `/boards/${encodeURIComponent(boardId)}/entity-exports/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/download`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: request.format === 'html'
              ? 'text/html,application/octet-stream'
              : 'text/markdown,text/plain,application/octet-stream',
          },
          body: JSON.stringify(request),
          signal,
        },
      );
      if (!response.ok) throw await responseError(response);
      const blob = await response.blob();
      if (blob.size === 0) throw new Error('The export service returned an empty attachment.');
      if (request.format === 'html') {
        assertPassiveStandaloneHtml(await blob.text());
      }
      const extension = request.format === 'html' ? 'html' : 'md';
      const fallback = `${entityType}_${fallbackTitle}_${request.scope}.${extension}`
        .toLowerCase()
        .replace(/[^a-z0-9._-]+/g, '-');
      return {
        blob,
        filename: safeDownloadFilename(
          contentDispositionFilename(response.headers.get('Content-Disposition')),
          fallback,
        ),
        content_type: response.headers.get('Content-Type') || blob.type,
        snapshot_fingerprint: response.headers.get('X-Export-Snapshot-Fingerprint'),
      };
    },
  }), [apiClient]);
}
