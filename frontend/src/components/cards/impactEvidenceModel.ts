// Pure draft model for the shared impact-evidence editor (SK-B2-S1,
// FR-7/AC-10). Kept out of the component file so both conclusion surfaces can
// import the serializer without dragging a component along — and so fast
// refresh keeps working for the editor itself.
import type {
  ImpactEvidence,
  ImpactEvidenceChangeKind,
  ImpactEvidenceRepo,
  ImpactEvidenceSurfaceKind,
  ImpactEvidenceSymbolAction,
  ImpactEvidenceSymbolKind,
  ImpactEvidenceTestAction,
} from '@/types';

export interface ImpactFileRow {
  repo: ImpactEvidenceRepo;
  path: string;
  change_kind: ImpactEvidenceChangeKind;
  previous_path: string;
  note: string;
}

export interface ImpactSymbolRow {
  name: string;
  kind: ImpactEvidenceSymbolKind;
  action: ImpactEvidenceSymbolAction;
  repo: ImpactEvidenceRepo;
  file: string;
}

export interface ImpactSurfaceRow {
  kind: ImpactEvidenceSurfaceKind;
  identifier: string;
}

export interface ImpactTestRow {
  action: ImpactEvidenceTestAction;
  repo: ImpactEvidenceRepo;
  test_file_path: string;
  test_function: string;
  scenario_id: string;
}

export interface ImpactEvidenceDraft {
  files: ImpactFileRow[];
  symbols: ImpactSymbolRow[];
  surfaces: ImpactSurfaceRow[];
  tests: ImpactTestRow[];
  evidence_refs: string[];
}

export function emptyImpactEvidenceDraft(): ImpactEvidenceDraft {
  return { files: [], symbols: [], surfaces: [], tests: [], evidence_refs: [] };
}

export function impactDraftRowCount(draft: ImpactEvidenceDraft): number {
  return (
    draft.files.length
    + draft.symbols.length
    + draft.surfaces.length
    + draft.tests.length
    + draft.evidence_refs.length
  );
}

/** A report almost never crosses repos row by row: carry the last choice. */
export function nextImpactRepo(
  rows: readonly { repo: ImpactEvidenceRepo }[],
): ImpactEvidenceRepo {
  return rows.length ? rows[rows.length - 1].repo : 'core';
}

/** AC-10: a submit with zero rows returns undefined — the move payload then
 * omits the field entirely instead of sending an empty block. */
export function buildImpactEvidencePayload(
  draft: ImpactEvidenceDraft,
): ImpactEvidence | undefined {
  if (impactDraftRowCount(draft) === 0) return undefined;
  return {
    schema_version: 1,
    files: draft.files.map((row) => ({
      repo: row.repo,
      path: row.path.trim(),
      change_kind: row.change_kind,
      ...(row.change_kind === 'renamed' && row.previous_path.trim()
        ? { previous_path: row.previous_path.trim() }
        : {}),
      ...(row.note.trim() ? { note: row.note.trim() } : {}),
    })),
    symbols: draft.symbols.map((row) => ({
      name: row.name.trim(),
      kind: row.kind,
      action: row.action,
      repo: row.repo,
      file: row.file.trim(),
    })),
    surfaces: draft.surfaces.map((row) => ({
      kind: row.kind,
      identifier: row.identifier.trim(),
    })),
    tests: draft.tests.map((row) => ({
      action: row.action,
      repo: row.repo,
      test_file_path: row.test_file_path.trim(),
      ...(row.test_function.trim()
        ? { test_function: row.test_function.trim() }
        : {}),
      ...(row.scenario_id.trim()
        ? { scenario_id: row.scenario_id.trim() }
        : {}),
    })),
    evidence_refs: draft.evidence_refs
      .map((ref) => ref.trim())
      .filter(Boolean),
  };
}
