export type EntityExportType =
  | 'story'
  | 'ideation'
  | 'refinement'
  | 'spec'
  | 'sprint'
  | 'card';

export type EntityExportFormat = 'markdown' | 'html';
export type EntityExportScope = 'current' | 'complete';

export type EntityExportSectionState =
  | 'included'
  | 'empty'
  | 'omitted'
  | 'unavailable'
  | 'not_applicable'
  | 'error';

export interface EntityExportIdentity {
  entity_type: EntityExportType;
  entity_id: string;
  title: string;
  status?: string | null;
  edition?: number | null;
  version?: number | null;
}

export interface EntityExportManifestSection {
  section_key: string;
  label: string;
  state: EntityExportSectionState;
  reason_code?: string | null;
  message?: string | null;
  total_count?: number | null;
}

/**
 * Permission-aware, version-fenced preview of a future report download.
 *
 * `complete_for_actor` means every source visible to the current actor was
 * captured. `source_complete` is stronger: no source was permission-omitted.
 * The UI deliberately presents both instead of calling an actor-limited
 * report globally complete.
 */
export interface EntityExportPreflight {
  schema_version: string;
  formats: EntityExportFormat[];
  scope: EntityExportScope;
  identity: EntityExportIdentity;
  snapshot_fingerprint: string;
  sections: EntityExportManifestSection[];
  complete_for_actor: boolean;
  source_complete: boolean;
}

export interface EntityExportPreflightRequest {
  scope: EntityExportScope;
  sections?: string[];
}

export interface EntityExportDownloadRequest extends EntityExportPreflightRequest {
  format: EntityExportFormat;
  expected_snapshot_fingerprint?: string;
}

export interface EntityExportDownload {
  blob: Blob;
  filename: string;
  content_type: string;
  snapshot_fingerprint?: string | null;
}
