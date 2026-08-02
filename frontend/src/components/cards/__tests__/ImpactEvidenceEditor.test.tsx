import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  ImpactEvidenceEditor,
  buildImpactEvidencePayload,
  emptyImpactEvidenceDraft,
  impactDraftRowCount,
  type ImpactEvidenceDraft,
} from '../ImpactEvidenceEditor';

describe('ImpactEvidenceEditor (SK-B2-S1 shared surface)', () => {
  it('starts collapsed with zero rows and never pre-populates content', () => {
    render(
      <ImpactEvidenceEditor
        draft={emptyImpactEvidenceDraft()}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('impact-evidence-row-count')).toHaveTextContent(
      '0',
    );
    const details = screen.getByTestId('impact-evidence-editor');
    expect(details).not.toHaveAttribute('open');
  });

  it('Add creates an empty row and Remove deletes the LAST remaining row', () => {
    const onChange = vi.fn();
    const draft: ImpactEvidenceDraft = {
      ...emptyImpactEvidenceDraft(),
      files: [
        {
          repo: 'core',
          path: 'src/a.py',
          change_kind: 'modified',
          previous_path: '',
          note: '',
        },
      ],
    };
    render(<ImpactEvidenceEditor draft={draft} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText('remove file 0'));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ files: [] }),
    );
    fireEvent.click(screen.getByTestId('impact-add-symbol'));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        symbols: [
          {
            name: '',
            kind: 'function',
            action: 'created',
            repo: 'core',
            file: '',
          },
        ],
      }),
    );
  });

  it('renamed change kind exposes the previous_path input', () => {
    const draft: ImpactEvidenceDraft = {
      ...emptyImpactEvidenceDraft(),
      files: [
        {
          repo: 'core',
          path: 'src/new.py',
          change_kind: 'renamed',
          previous_path: '',
          note: '',
        },
      ],
    };
    render(<ImpactEvidenceEditor draft={draft} onChange={vi.fn()} />);
    expect(screen.getByLabelText('file 0 previous path')).toBeInTheDocument();
  });
});

describe('buildImpactEvidencePayload (AC-10)', () => {
  it('returns undefined for zero rows so the move omits the field', () => {
    expect(buildImpactEvidencePayload(emptyImpactEvidenceDraft())).toBe(
      undefined,
    );
  });

  it('builds schema v1 with trimmed values and omitted empty optionals', () => {
    const draft: ImpactEvidenceDraft = {
      files: [
        {
          repo: 'community',
          path: '  frontend/src/x.tsx  ',
          change_kind: 'created',
          previous_path: '',
          note: '',
        },
      ],
      symbols: [],
      surfaces: [{ kind: 'ui_component', identifier: ' CardModal ' }],
      tests: [
        {
          action: 'added',
          repo: 'community',
          test_file_path: 'frontend/src/__tests__/x.test.tsx',
          test_function: '',
          scenario_id: ' ts_abc ',
        },
      ],
      evidence_refs: [' ts_abc ', ''],
    };
    const payload = buildImpactEvidencePayload(draft);
    expect(payload).toEqual({
      schema_version: 1,
      files: [
        {
          repo: 'community',
          path: 'frontend/src/x.tsx',
          change_kind: 'created',
        },
      ],
      symbols: [],
      surfaces: [{ kind: 'ui_component', identifier: 'CardModal' }],
      tests: [
        {
          action: 'added',
          repo: 'community',
          test_file_path: 'frontend/src/__tests__/x.test.tsx',
          scenario_id: 'ts_abc',
        },
      ],
      evidence_refs: ['ts_abc'],
    });
    expect(impactDraftRowCount(draft)).toBe(5);
  });

  it('previous_path only ships for renamed files', () => {
    const renamed = buildImpactEvidencePayload({
      ...emptyImpactEvidenceDraft(),
      files: [
        {
          repo: 'core',
          path: 'src/new.py',
          change_kind: 'renamed',
          previous_path: 'src/old.py',
          note: '',
        },
      ],
    });
    expect(renamed?.files[0].previous_path).toBe('src/old.py');
    const modified = buildImpactEvidencePayload({
      ...emptyImpactEvidenceDraft(),
      files: [
        {
          repo: 'core',
          path: 'src/new.py',
          change_kind: 'modified',
          previous_path: 'src/old.py',
          note: '',
        },
      ],
    });
    expect(modified?.files[0]).not.toHaveProperty('previous_path');
  });
});
