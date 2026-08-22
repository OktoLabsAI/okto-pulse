import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { EntityExportPreflight } from '@/types/entity-export';

import { EntityExportButton } from './EntityExportButton';

const FINGERPRINT = 'a'.repeat(64);
const SELECTION_FINGERPRINT = 'b'.repeat(64);

const mocks = vi.hoisted(() => ({
  preflight: vi.fn(),
  download: vi.fn(),
}));

vi.mock('@/services/entity-export-api', () => ({
  useEntityExportApi: () => mocks,
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

function preflight(overrides: Partial<EntityExportPreflight> = {}): EntityExportPreflight {
  return {
    schema_version: 'entity-export/v1',
    formats: ['markdown', 'html'],
    scope: 'complete',
    identity: {
      entity_type: 'spec',
      entity_id: 'spec-1',
      title: 'Export Spec',
      status: 'approved',
      edition: 3,
      version: 18,
    },
    snapshot_fingerprint: FINGERPRINT,
    sections: [
      { section_key: 'identity', label: 'Identity', state: 'included', total_count: 1 },
      { section_key: 'policy', label: 'Policy Compliance', state: 'omitted', reason_code: 'permission_denied' },
    ],
    complete_for_actor: true,
    source_complete: false,
    ...overrides,
  };
}

describe('EntityExportDialog', () => {
  beforeEach(() => {
    mocks.preflight.mockReset();
    mocks.download.mockReset();
    mocks.preflight.mockResolvedValue(preflight());
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  });

  it('shows the permission-aware manifest and does not claim global completeness', async () => {
    render(
      <div role="dialog" aria-modal="true">
        <EntityExportButton
          boardId="board-1"
          entityType="spec"
          entityId="spec-1"
          entityTitle="Export Spec"
        />
      </div>,
    );
    fireEvent.click(screen.getByTitle('Export report'));

    expect(await screen.findByRole('heading', { name: 'Export Report' })).toBeInTheDocument();
    expect(screen.getByText('Export Spec')).toBeInTheDocument();
    expect(screen.getByText('Spec')).toBeInTheDocument();
    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText('Edition 3 / Revision 18')).toBeInTheDocument();
    expect(await screen.findByText('Policy Compliance')).toBeInTheDocument();
    expect(screen.getByText('Permission omitted')).toBeInTheDocument();
    expect(screen.getByText(/complete for your access, not globally complete/i)).toBeInTheDocument();
    expect(mocks.preflight).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      { scope: 'complete' },
      expect.any(AbortSignal),
    );
  });

  it('blocks a false-complete download when selected sources are unavailable', async () => {
    mocks.preflight.mockResolvedValue(preflight({
      complete_for_actor: false,
      source_complete: false,
      sections: [{
        section_key: 'validation',
        label: 'Validation',
        state: 'unavailable',
        reason_code: 'source_timeout',
      }],
    }));
    render(
      <EntityExportButton
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
        entityTitle="Export Spec"
      />,
    );
    fireEvent.click(screen.getByTitle('Export report'));
    expect(await screen.findByText('Unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download Markdown' })).toBeDisabled();
    expect(screen.getByText(/Download is blocked/i)).toBeInTheDocument();
  });

  it('uses a passive structural HTML preview and downloads with the preflight fence', async () => {
    mocks.preflight.mockResolvedValue(preflight({ source_complete: true }));
    mocks.download.mockResolvedValue({
      blob: new Blob(['<!doctype html><p>server report</p>'], { type: 'text/html' }),
      filename: 'spec-export.html',
      content_type: 'text/html',
    });
    render(
      <EntityExportButton
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
        entityTitle="Export Spec"
      />,
    );
    fireEvent.click(screen.getByTitle('Export report'));
    await screen.findByText('Policy Compliance');
    fireEvent.click(screen.getByRole('button', { name: /Rich HTML/i }));
    fireEvent.click(screen.getByRole('tab', { name: 'Preview & manifest' }));
    const frame = screen.getByTitle('HTML report structural preview');
    expect(frame).toHaveAttribute('sandbox', '');
    expect(frame.getAttribute('srcdoc')).not.toMatch(/<script\b/i);
    expect(frame.getAttribute('srcdoc')).not.toContain(FINGERPRINT);
    const includedSummary = screen.getByText('Included', { selector: 'div' }).parentElement;
    expect(includedSummary).not.toBeNull();
    expect(within(includedSummary!).getByText('1 section')).toBeInTheDocument();
    const metadataSummary = screen.getByText('Technical export metadata');
    expect(metadataSummary.closest('details')).not.toHaveAttribute('open');
    expect(screen.queryByText(FINGERPRINT.slice(0, 12), { exact: true })).not.toBeInTheDocument();
    fireEvent.click(metadataSummary);
    expect(metadataSummary.closest('details')).toHaveAttribute('open');
    expect(screen.getByText('spec-1')).toBeInTheDocument();
    expect(screen.getByText(FINGERPRINT)).toBeInTheDocument();
    expect(mocks.preflight).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Download HTML' }));
    await waitFor(() => expect(mocks.download).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      expect.objectContaining({
        format: 'html',
        scope: 'complete',
        expected_snapshot_fingerprint: FINGERPRINT,
      }),
      'Export Spec',
      expect.any(AbortSignal),
    ));
  });

  it('seals the exact selected sections before issuing the attachment request', async () => {
    const initial = preflight({
      source_complete: true,
      sections: [
        { section_key: 'base', label: 'Identity', state: 'included', total_count: 1 },
        { section_key: 'requirements', label: 'Requirements', state: 'included', total_count: 3 },
      ],
    });
    const selected = preflight({
      source_complete: true,
      snapshot_fingerprint: SELECTION_FINGERPRINT,
      sections: [
        { section_key: 'base', label: 'Identity', state: 'included', total_count: 1 },
        { section_key: 'requirements', label: 'Requirements', state: 'omitted', reason_code: 'not_requested' },
      ],
    });
    mocks.preflight.mockResolvedValueOnce(initial).mockResolvedValueOnce(selected);
    mocks.download.mockResolvedValue({
      blob: new Blob(['# Report'], { type: 'text/markdown' }),
      filename: 'spec-export.md',
      content_type: 'text/markdown',
    });

    render(
      <EntityExportButton
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
        entityTitle="Export Spec"
      />,
    );
    fireEvent.click(screen.getByTitle('Export report'));
    const requirementsLabel = await screen.findByText('Requirements');
    const requirements = requirementsLabel.closest('button');
    expect(requirements).not.toBeNull();
    fireEvent.click(requirements!);
    fireEvent.click(screen.getByRole('button', { name: 'Download Markdown' }));

    await waitFor(() => expect(mocks.preflight).toHaveBeenLastCalledWith(
      'board-1',
      'spec',
      'spec-1',
      { scope: 'complete', sections: ['base'] },
      expect.any(AbortSignal),
    ));
    expect(mocks.download).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      expect.objectContaining({
        sections: ['base'],
        expected_snapshot_fingerprint: SELECTION_FINGERPRINT,
      }),
      'Export Spec',
      expect.any(AbortSignal),
    );
  });

  it('surfaces preflight failures and retries without a silent fallback', async () => {
    mocks.preflight
      .mockRejectedValueOnce(new Error('preflight exploded'))
      .mockResolvedValueOnce(preflight());
    render(
      <EntityExportButton
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
        entityTitle="Export Spec"
      />,
    );

    fireEvent.click(screen.getByTitle('Export report'));
    expect(await screen.findByRole('alert')).toHaveTextContent('preflight exploded');
    expect(screen.getByRole('button', { name: 'Retry preflight' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download Markdown' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Retry preflight' }));
    expect(await screen.findByText('Policy Compliance')).toBeInTheDocument();
    expect(mocks.preflight).toHaveBeenCalledTimes(2);
  });

  it('traps initial focus, closes only the nested layer on Escape, and restores its opener', async () => {
    render(
      <div data-testid="parent-dialog" role="dialog" aria-modal="true">
        <EntityExportButton
          boardId="board-1"
          entityType="spec"
          entityId="spec-1"
          entityTitle="Export Spec"
        />
      </div>,
    );
    const opener = screen.getByTitle('Export report');
    opener.focus();
    fireEvent.click(opener);
    await screen.findByText('Policy Compliance');

    await waitFor(() => expect(screen.getByRole('button', { name: /^Markdown/i })).toHaveFocus());
    expect(screen.getByTestId('parent-dialog')).not.toHaveAttribute('aria-modal');

    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Export Report' })).not.toBeInTheDocument());
    expect(screen.getByTestId('parent-dialog')).toHaveAttribute('aria-modal', 'true');
    expect(opener).toHaveFocus();
  });
});
