// Spec 8a2fad91 / card 5cb88511 / FR5+AC7 — the Guidelines UI surfaces default
// state from the REAL umbrella template and BLOCKS the Set-default action for
// inline guidelines (only global catalog guidelines are eligible defaults).
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  getBoardGuidelines: vi.fn(),
  listDefaultGuidelineCandidates: vi.fn(),
  updateDefaultGuidelineRefs: vi.fn(),
  createDefaultBoardConfigVersion: vi.fn(),
  linkGuidelineToBoard: vi.fn(),
  unlinkGuidelineFromBoard: vi.fn(),
  // unused-on-mount methods referenced by the panel; stubbed defensively.
  listGuidelines: vi.fn().mockResolvedValue([]),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

import { GuidelinesPanel } from '../GuidelinesPanel';

describe('GuidelinesPanel guideline defaults', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getBoardGuidelines.mockResolvedValue([
      { id: 'e1', priority: 0, scope: 'global', guideline: { id: 'g1', title: 'Global G', content: 'c', tags: null, version: 1 } },
      { id: 'e2', priority: 1, scope: 'inline', guideline: { id: 'g2', title: 'Inline G', content: 'c', tags: null, version: 1 } },
    ]);
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: 't1', template_version: 1,
      candidates: [
        { guideline_id: 'g1', title: 'Global G', scope: 'global', guideline_version: 1, eligible: true, is_default: false, priority: null },
      ],
    });
    apiMock.listGuidelines.mockResolvedValue([]);
    apiMock.updateDefaultGuidelineRefs.mockResolvedValue({});
    apiMock.linkGuidelineToBoard.mockResolvedValue({});
    apiMock.unlinkGuidelineFromBoard.mockResolvedValue({});
    apiMock.createDefaultBoardConfigVersion.mockResolvedValue({
      id: 'created-template',
      version: 1,
      status: 'active',
      is_active: true,
      scope: 'global',
      settings_payload: {},
      guideline_default_refs: [],
      design_system_default_ref: null,
      created_by: 'u',
      created_at: null,
      updated_at: null,
    });
  });

  it('blocks Set-default for inline guidelines and enables it for global ones', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    const globalBtn = await screen.findByTestId('guideline-set-default-g1');
    const inlineBtn = screen.getByTestId('guideline-set-default-g2');

    expect(inlineBtn).toBeDisabled();        // inline guidelines cannot be defaults (AC7)
    expect(globalBtn).not.toBeDisabled();

    fireEvent.click(globalBtn);
    await waitFor(() => expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalled());
    const [tplId, refs] = apiMock.updateDefaultGuidelineRefs.mock.calls[0];
    expect(tplId).toBe('t1');
    expect(refs).toEqual([{ guideline_id: 'g1', priority: 1 }]);
  });

  it('shows the current default state from the template, not a local flag', async () => {
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: 't1', template_version: 1,
      candidates: [
        { guideline_id: 'g1', title: 'Global G', scope: 'global', guideline_version: 1, eligible: true, is_default: true, priority: 2 },
      ],
    });
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    const globalBtn = await screen.findByTestId('guideline-set-default-g1');
    expect(globalBtn.textContent).toContain('Default');
  });

  it('exposes Set-default from the Global Catalog tab', async () => {
    apiMock.listGuidelines.mockResolvedValue([
      { id: 'g1', title: 'Global G', content: 'global content', tags: null, version: 1, scope: 'global' },
    ]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Global Catalog'));
    const globalBtn = await screen.findByTestId('guideline-set-default-g1');

    expect(globalBtn).not.toBeDisabled();
    fireEvent.click(globalBtn);
    await waitFor(() => expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalled());
    expect(apiMock.updateDefaultGuidelineRefs.mock.calls[0][1]).toEqual([
      { guideline_id: 'g1', priority: 1 },
    ]);
  });

  it('keeps board linking actions in the Global Catalog, not Board Guidelines', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));

    expect(screen.queryByText('Link Global')).not.toBeInTheDocument();
    expect(screen.getByText('Create Inline')).toBeInTheDocument();
  });

  it('links and unlinks global catalog guidelines to the current board', async () => {
    apiMock.listGuidelines.mockResolvedValue([
      { id: 'g1', title: 'Linked Global', content: 'linked content', tags: null, version: 1, scope: 'global' },
      { id: 'g3', title: 'Available Global', content: 'available content', tags: null, version: 1, scope: 'global' },
    ]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);

    fireEvent.click(await screen.findByTestId('guideline-link-board-g3'));
    await waitFor(() => expect(apiMock.linkGuidelineToBoard).toHaveBeenCalledWith('b1', 'g3'));

    fireEvent.click(await screen.findByTestId('guideline-unlink-board-g1'));
    await waitFor(() => expect(apiMock.unlinkGuidelineFromBoard).toHaveBeenCalledWith('b1', 'g1'));
  });

  it('updates the visible default state after setting a guideline as default', async () => {
    apiMock.listGuidelines.mockResolvedValue([
      { id: 'g1', title: 'Global G', content: 'global content', tags: null, version: 1, scope: 'global' },
    ]);
    apiMock.listDefaultGuidelineCandidates
      .mockResolvedValueOnce({
        scope: 'global', template_id: 't1', template_version: 1,
        candidates: [
          { guideline_id: 'g1', title: 'Global G', scope: 'global', guideline_version: 1, eligible: true, is_default: false, priority: null },
        ],
      })
      .mockResolvedValueOnce({
        scope: 'global', template_id: 't1', template_version: 2,
        candidates: [
          { guideline_id: 'g1', title: 'Global G', scope: 'global', guideline_version: 1, eligible: true, is_default: true, priority: 1 },
        ],
      });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const globalBtn = await screen.findByTestId('guideline-set-default-g1');
    expect(globalBtn.textContent).toContain('Set default');

    fireEvent.click(globalBtn);

    await waitFor(() => expect(globalBtn.textContent).toContain('Default'));
    expect(apiMock.listDefaultGuidelineCandidates).toHaveBeenCalledTimes(2);
  });

  it('creates an active template before setting a default when none exists', async () => {
    apiMock.listGuidelines.mockResolvedValue([
      { id: 'g1', title: 'Global G', content: 'global content', tags: null, version: 1, scope: 'global' },
    ]);
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: null, template_version: null,
      candidates: [
        { guideline_id: 'g1', title: 'Global G', scope: 'global', guideline_version: 1, eligible: true, is_default: false, priority: null },
      ],
    });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guideline-set-default-g1'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith({ activate: true }));
    expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalledWith('created-template', [
      { guideline_id: 'g1', priority: 1 },
    ]);
  });

  it('shows help examples for guideline assistant context', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guideline-help-toggle'));

    expect(await screen.findByTestId('guideline-help-examples')).toHaveTextContent('Assistant context examples');
    expect(screen.getByText(/Board workflow/i)).toBeInTheDocument();
  });
});
