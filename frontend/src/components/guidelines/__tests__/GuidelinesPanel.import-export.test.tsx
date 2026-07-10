// ITEM 19 — GuidelinesPanel Export/Import buttons: Export hits GET
// /guidelines/export and triggers a blob download (guidelines-YYYYMMDD.json);
// Import parses the picked .json file, POSTs the envelope to
// /guidelines/import and reports created/skipped/errors via toast.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';

const apiMock = vi.hoisted(() => ({
  getBoardGuidelines: vi.fn(),
  listDefaultGuidelineCandidates: vi.fn(),
  listGuidelines: vi.fn(),
}));
// The panel's data layer is mocked; the import-export service runs FOR REAL
// against a mocked AuthenticatedFetch so the test covers the actual
// endpoint paths, the blob download and the toast summary.
const fetchJsonMock = vi.hoisted(() => vi.fn());
vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => ({ fetchJson: fetchJsonMock }),
}));
vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import { GuidelinesPanel } from '../GuidelinesPanel';

const ENVELOPE = {
  schema_version: '1',
  kind: 'guidelines',
  exported_at: '2026-07-10T00:00:00+00:00',
  items: [
    { title: 'Global rule', content: 'c', tags: null, scope: 'global', board_id: null },
    { title: 'Inline rule', content: 'c', tags: null, scope: 'inline', board_id: 'b1' },
  ],
};

describe('GuidelinesPanel import/export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoardGuidelines.mockResolvedValue([]);
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: null, template_version: null, candidates: [],
    });
    apiMock.listGuidelines.mockResolvedValue([]);
    // jsdom has no object-URL implementation.
    URL.createObjectURL = vi.fn(() => 'blob:mock-url');
    URL.revokeObjectURL = vi.fn();
  });

  it('exports the guidelines envelope as a dated .json blob download', async () => {
    fetchJsonMock.mockResolvedValueOnce(ENVELOPE);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guidelines-export'));

    await waitFor(() =>
      expect(fetchJsonMock).toHaveBeenCalledWith('/guidelines/export?board_id=b1'),
    );
    // A blob URL was created and the download anchor was clicked with the
    // kind-YYYYMMDD.json filename.
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    const blob = (URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls[0][0] as Blob;
    expect(blob.type).toBe('application/json');
    expect(await blob.text()).toBe(JSON.stringify(ENVELOPE, null, 2));
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const anchor = clickSpy.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toMatch(/^guidelines-\d{8}\.json$/);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
    expect(toast.success).toHaveBeenCalledWith('Exported 2 items');
    clickSpy.mockRestore();
  });

  it('imports a picked .json file via POST and toasts the created/skipped summary', async () => {
    fetchJsonMock.mockResolvedValueOnce({
      created: 1,
      skipped: [{ index: 0, title: 'Global rule', reason: 'duplicate_global_title' }],
      errors: [],
      dry_run: false,
    });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const refreshCallsBefore = apiMock.getBoardGuidelines.mock.calls.length;
    const input = await screen.findByTestId('guidelines-import-input');
    const file = new File([JSON.stringify(ENVELOPE)], 'guidelines-20260710.json', {
      type: 'application/json',
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(fetchJsonMock).toHaveBeenCalledWith('/guidelines/import?board_id=b1', {
        method: 'POST',
        body: JSON.stringify(ENVELOPE),
      }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Import: 1 created, 1 skipped'),
    );
    // The panel refreshes its lists after a successful import.
    await waitFor(() =>
      expect(apiMock.getBoardGuidelines.mock.calls.length).toBeGreaterThan(refreshCallsBefore),
    );
  });

  it('surfaces a backend 400 (invalid item, nothing mutated) as a toast error', async () => {
    fetchJsonMock.mockRejectedValueOnce(new Error('HTTP 400: Bad Request'));

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const input = await screen.findByTestId('guidelines-import-input');
    const file = new File([JSON.stringify(ENVELOPE)], 'guidelines.json', {
      type: 'application/json',
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('HTTP 400: Bad Request'));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('rejects a non-JSON file locally without calling the API', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const input = await screen.findByTestId('guidelines-import-input');
    const file = new File(['not json {'], 'broken.json', { type: 'application/json' });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Invalid file: not valid JSON'),
    );
    expect(fetchJsonMock).not.toHaveBeenCalled();
  });
});
