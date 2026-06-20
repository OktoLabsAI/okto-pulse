// Spec 3a006f65 / card 1392f59d / ts_1054bf42 — the Design System menu renders the
// global catalog, this board's inline systems, and the effective Design System from
// the REAL catalog API (mocked at the api-service boundary, never a local mock
// state), and the admin actions (create global/inline, link) hit the real API.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  listDesignSystems: vi.fn(),
  getBoardDesignSystem: vi.fn(),
  getActiveDefaultBoardConfig: vi.fn(),
  createDesignSystem: vi.fn(),
  updateDesignSystem: vi.fn(),
  deleteDesignSystem: vi.fn(),
  linkBoardDesignSystem: vi.fn(),
  unlinkBoardDesignSystem: vi.fn(),
  setDefaultDesignSystem: vi.fn(),
  createDefaultBoardConfigVersion: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));

import { DesignSystemPanel } from '../DesignSystemPanel';

function ds(over: Record<string, unknown> = {}) {
  return {
    id: 'g1', scope: 'global', board_id: null, title: 'DS1', payload: null,
    version: 1, status: 'active', owner_id: 'u', created_at: null, updated_at: null, ...over,
  };
}

describe('DesignSystemPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listDesignSystems.mockImplementation((scope: string) =>
      scope === 'global'
        ? Promise.resolve([ds({ id: 'g1', title: 'DS1', payload: { content: 'Use compact controls.' } }), ds({ id: 'g2', title: 'DS2' })])
        : Promise.resolve([ds({ id: 'i1', scope: 'inline', board_id: 'b1', title: 'Inline DS', payload: { content: 'Board-only rule.' } })]),
    );
    apiMock.getBoardDesignSystem.mockResolvedValue({
      board_id: 'b1', effective: { source: 'board_link', design_system_id: 'g1', version: 1 },
    });
    apiMock.getActiveDefaultBoardConfig.mockResolvedValue({
      scope: 'global',
      active: {
        id: 'tpl-1',
        version: 3,
        status: 'active',
        is_active: true,
        scope: 'global',
        settings_payload: { design_system_gate_mode: 'blocking' },
        guideline_default_refs: [],
        design_system_default_ref: { design_system_id: 'g1', version: 1, gate_mode: 'blocking' },
        created_by: 'u',
        created_at: null,
        updated_at: null,
      },
    });
    apiMock.createDesignSystem.mockResolvedValue(ds());
    apiMock.updateDesignSystem.mockResolvedValue(ds());
    apiMock.deleteDesignSystem.mockResolvedValue(undefined);
    apiMock.linkBoardDesignSystem.mockResolvedValue({});
    apiMock.unlinkBoardDesignSystem.mockResolvedValue(undefined);
    apiMock.setDefaultDesignSystem.mockResolvedValue({});
    apiMock.createDefaultBoardConfigVersion.mockResolvedValue({
      id: 'tpl-created',
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

  it('renders global catalog, board inline, and effective from REAL api data', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    await screen.findByTestId('design-system-panel');

    // The effective/linked Design System (g1) is surfaced via the "linked" badge in the
    // catalog now that the status cards (Active default / Board snapshot / Board gate) and
    // the Mockup submission panel were removed from the cadastro view.
    expect(await screen.findByTestId('dsp-global-g1')).toBeInTheDocument();
    expect(screen.getByTestId('dsp-global-g2')).toBeInTheDocument();
    expect(apiMock.listDesignSystems).toHaveBeenCalledWith('inline', 'b1');

    expect(screen.getByTestId('dsp-linked-g1')).toBeInTheDocument();
    expect(screen.getByTestId('dsp-default-g1')).toBeInTheDocument();
    expect(screen.getByTestId('dsp-link-g2')).toBeInTheDocument();
    expect(screen.queryByTestId('dsp-default-gate-mode')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dsp-tab-gate')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dsp-tab-board'));
    await screen.findByTestId('dsp-inline-i1');
    expect(screen.getByTestId('dsp-inline-i1')).toBeInTheDocument();
  });

  it('creates a global Design System with assistant context content', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('New design system'));
    await screen.findByTestId('dsp-create-global');
    fireEvent.change(screen.getByTestId('dsp-new-title'), { target: { value: 'New DS' } });
    fireEvent.change(screen.getByTestId('dsp-new-content'), { target: { value: 'Use 8px radius and compact forms.' } });
    fireEvent.click(screen.getByTestId('dsp-create-global'));
    await waitFor(() =>
      expect(apiMock.createDesignSystem).toHaveBeenCalledWith({
        title: 'New DS',
        scope: 'global',
        payload: { content: 'Use 8px radius and compact forms.' },
      }),
    );
  });

  it('creates an inline Design System bound to the board', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-tab-board'));
    fireEvent.click(await screen.findByText('Create Inline'));
    await screen.findByTestId('dsp-create-inline');
    fireEvent.change(screen.getByTestId('dsp-new-title'), { target: { value: 'Inline X' } });
    fireEvent.change(screen.getByTestId('dsp-new-content'), { target: { value: '{"tokens":{"radius":"8px"}}' } });
    fireEvent.click(screen.getByTestId('dsp-create-inline'));
    await waitFor(() =>
      expect(apiMock.createDesignSystem).toHaveBeenCalledWith({
        title: 'Inline X',
        scope: 'inline',
        board_id: 'b1',
        payload: { tokens: { radius: '8px' } },
      }),
    );
  });

  it('updates an existing Design System content payload', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-edit-g1'));
    await screen.findByTestId('dsp-save-edit');
    fireEvent.change(screen.getByTestId('dsp-new-content'), { target: { value: 'Updated assistant context.' } });
    fireEvent.click(screen.getByTestId('dsp-save-edit'));
    await waitFor(() =>
      expect(apiMock.updateDesignSystem).toHaveBeenCalledWith('g1', {
        title: 'DS1',
        payload: { content: 'Updated assistant context.' },
      }),
    );
  });

  it('links a non-effective global Design System to the board', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    const useBtn = await screen.findByTestId('dsp-link-g2');
    fireEvent.click(useBtn);
    await waitFor(() =>
      expect(apiMock.linkBoardDesignSystem).toHaveBeenCalledWith('b1', 'g2'),
    );
  });

  it('sets a global Design System as default through the active template', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-set-default-g2'));
    await waitFor(() =>
      expect(apiMock.setDefaultDesignSystem).toHaveBeenCalledWith('tpl-1', {
        design_system_id: 'g2',
        version: 1,
        gate_mode: 'blocking',
      }),
    );
  });

  it('creates an active template before setting a default when none exists', async () => {
    apiMock.getActiveDefaultBoardConfig.mockResolvedValue({
      scope: 'global',
      active: null,
    });

    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-set-default-g2'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith({ activate: true }));
    expect(apiMock.setDefaultDesignSystem).toHaveBeenCalledWith('tpl-created', {
      design_system_id: 'g2',
      version: 1,
      gate_mode: 'off',
    });
  });

  it('shows help examples for contextualizing assistant output', async () => {
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-help-toggle'));
    expect(await screen.findByTestId('dsp-help-examples')).toHaveTextContent('Assistant context examples');
    expect(screen.getByText(/Mockup evidence/i)).toBeInTheDocument();
  });

  it('lets you stop using (unlink) the selected Design System from its catalog row', async () => {
    // g1 is the explicit board link (source=board_link) -> its catalog row offers
    // Unlink, so the selection is reversible from where it was made.
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-unlink-g1'));
    await waitFor(() => expect(apiMock.unlinkBoardDesignSystem).toHaveBeenCalledWith('b1'));
  });

  it('renders the default fallback as "default" (not "linked") and keeps it reversible to Use', async () => {
    // No per-board link: the effective DS comes from the default snapshot. The row must
    // read as the default fallback, not as an explicit selection, and stay selectable.
    apiMock.getBoardDesignSystem.mockResolvedValue({
      board_id: 'b1',
      effective: { source: 'default_snapshot', design_system_id: 'g1', version: 1, gate_mode: 'advisory' },
    });
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    await screen.findByTestId('design-system-panel');

    expect(await screen.findByTestId('dsp-default-g1')).toBeInTheDocument();
    expect(screen.queryByTestId('dsp-linked-g1')).not.toBeInTheDocument();
    // Reversible: the fallback is not a dead end — Use is offered, Unlink is not.
    expect(screen.getByTestId('dsp-link-g1')).toBeInTheDocument();
    expect(screen.queryByTestId('dsp-unlink-g1')).not.toBeInTheDocument();

    // Board tab explains the fallback and offers NO unlink (nothing to remove).
    fireEvent.click(screen.getByTestId('dsp-tab-board'));
    expect(await screen.findByTestId('dsp-effective-default-note')).toBeInTheDocument();
    expect(screen.queryByTestId('dsp-unlink')).not.toBeInTheDocument();
  });

  it('unsets the default Design System through a new template version (no clear endpoint)', async () => {
    // g1 is the current default -> its Default button unsets it by copy-on-write a new
    // active version with design_system_default_ref cleared (there is no clear endpoint).
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-set-default-g1'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalled());
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(
      expect.objectContaining({ design_system_default_ref: null, activate: true }),
    );
    // Unsetting must NOT go through the set-default endpoint.
    expect(apiMock.setDefaultDesignSystem).not.toHaveBeenCalled();
  });

  it('deletes a Design System from the catalog after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-delete-g2'));
    await waitFor(() => expect(apiMock.deleteDesignSystem).toHaveBeenCalledWith('g2'));
    confirmSpy.mockRestore();
  });

  it('does not delete when the confirmation is dismissed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<DesignSystemPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('dsp-delete-g2'));
    expect(apiMock.deleteDesignSystem).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
