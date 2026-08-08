import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ImpactEvidenceEditor } from '../ImpactEvidenceEditor';
import {
  buildImpactEvidencePayload,
  emptyImpactEvidenceDraft,
  impactDraftRowCount,
  type ImpactEvidenceDraft,
} from '../impactEvidenceModel';

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

  it('note and test_function are reachable, not just serializable', () => {
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
      tests: [
        {
          action: 'added',
          repo: 'core',
          test_file_path: 'tests/test_a.py',
          test_function: '',
          scenario_id: '',
        },
      ],
    };
    render(<ImpactEvidenceEditor draft={draft} onChange={onChange} />);

    fireEvent.change(screen.getByLabelText('file 0 note'), {
      target: { value: 'moved the gate into the report_target block' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        files: [
          expect.objectContaining({
            note: 'moved the gate into the report_target block',
          }),
        ],
      }),
    );

    fireEvent.change(screen.getByLabelText('test 0 function'), {
      target: { value: 'test_require_mode_blocks_without_evidence' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        tests: [
          expect.objectContaining({
            test_function: 'test_require_mode_blocks_without_evidence',
          }),
        ],
      }),
    );
  });

  it('a new row inherits the repo of the previous one', () => {
    const onChange = vi.fn();
    const draft: ImpactEvidenceDraft = {
      ...emptyImpactEvidenceDraft(),
      files: [
        {
          repo: 'community',
          path: 'frontend/src/x.tsx',
          change_kind: 'modified',
          previous_path: '',
          note: '',
        },
      ],
    };
    render(<ImpactEvidenceEditor draft={draft} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('impact-add-file'));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        files: [
          expect.anything(),
          expect.objectContaining({ repo: 'community' }),
        ],
      }),
    );
    // An empty section still starts on core.
    fireEvent.click(screen.getByTestId('impact-add-test'));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        tests: [expect.objectContaining({ repo: 'core' })],
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
