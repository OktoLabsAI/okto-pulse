import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Header } from './Header';
import type { Board, BoardSettings } from '@/types';

const apiMock = vi.hoisted(() => ({
  updateBoard: vi.fn(),
}));

const boardState = vi.hoisted(() => ({
  currentBoard: null as Board | null,
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => boardState.currentBoard,
}));

vi.mock('@/adapters', () => ({
  authAdapter: {
    useAuth: () => ({ isSignedIn: false, isLoaded: true }),
    UserButton: null,
  },
  portalAdapter: {
    ShareBoardModal: null,
  },
}));

vi.mock('@/hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'light', toggle: vi.fn() }),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const baseSettings: BoardSettings = {
  max_scenarios_per_card: 3,
  skip_test_coverage_global: false,
  skip_rules_coverage_global: false,
  skip_trs_coverage_global: false,
  skip_contract_coverage_global: false,
  skip_ir_coverage_global: false,
  skip_or_coverage_global: false,
  skip_decisions_coverage_global: false,
  require_task_validation: true,
  min_confidence: 70,
  min_completeness: 80,
  max_drift: 50,
  require_spec_validation: true,
  min_spec_completeness: 80,
  min_spec_assertiveness: 80,
  max_spec_ambiguity: 30,
  require_spec_resource_task_coverage: true,
  skip_test_evidence_global: false,
  auto_derive_spec_resources_enabled: false,
  auto_derive_spec_resource_types: [],
};

function boardWith(settings: Partial<BoardSettings>): Board {
  return {
    id: 'board-1',
    name: 'Board One',
    description: null,
    owner_id: 'user-1',
    settings: { ...baseSettings, ...settings },
    created_at: '2026-05-14T00:00:00Z',
    updated_at: '2026-05-14T00:00:00Z',
    cards: [],
    agents: [],
  };
}

function renderOpenHeader() {
  render(<Header />);
  act(() => {
    window.dispatchEvent(new CustomEvent('okto:open-board-settings'));
  });
}

describe('Header Board settings resource automation', () => {
  beforeEach(() => {
    apiMock.updateBoard.mockReset();
    apiMock.updateBoard.mockResolvedValue({});
    boardState.currentBoard = boardWith({});
  });

  it('enabling automation with no selected types sends all resource types', async () => {
    renderOpenHeader();

    fireEvent.click(screen.getByTestId('toggle-spec-resource-automation'));

    await waitFor(() => expect(apiMock.updateBoard).toHaveBeenCalledTimes(1));
    expect(apiMock.updateBoard).toHaveBeenCalledWith(
      'board-1',
      expect.objectContaining({
        settings: expect.objectContaining({
          auto_derive_spec_resources_enabled: true,
          auto_derive_spec_resource_types: ['knowledge_base', 'architecture', 'mockup'],
        }),
      }),
    );
  });

  it('opens board settings in the standard modal shell', () => {
    renderOpenHeader();

    const modal = screen.getByTestId('board-settings-modal');

    expect(modal).toHaveClass('modal-content');
    expect(screen.getByRole('dialog', { name: 'Board' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close board settings' })).toBeInTheDocument();
  });

  it('does not send an invalid payload when removing the last active resource type', async () => {
    boardState.currentBoard = boardWith({
      auto_derive_spec_resources_enabled: true,
      auto_derive_spec_resource_types: ['knowledge_base'],
    });
    renderOpenHeader();

    fireEvent.click(screen.getByTestId('spec-resource-type-knowledge_base'));

    expect(apiMock.updateBoard).not.toHaveBeenCalled();
  });

  it('persists selected resource types while automation is active', async () => {
    boardState.currentBoard = boardWith({
      auto_derive_spec_resources_enabled: true,
      auto_derive_spec_resource_types: ['knowledge_base'],
    });
    renderOpenHeader();

    fireEvent.click(screen.getByTestId('spec-resource-type-architecture'));

    await waitFor(() => expect(apiMock.updateBoard).toHaveBeenCalledTimes(1));
    expect(apiMock.updateBoard).toHaveBeenCalledWith(
      'board-1',
      expect.objectContaining({
        settings: expect.objectContaining({
          auto_derive_spec_resources_enabled: true,
          auto_derive_spec_resource_types: ['knowledge_base', 'architecture'],
        }),
      }),
    );
  });
});
