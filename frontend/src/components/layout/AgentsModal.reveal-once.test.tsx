import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentsModal } from './AgentsModal';
import type { Agent, PermissionPreset } from '@/types';

const apiMock = vi.hoisted(() => ({
  createAgent: vi.fn(),
  deleteAgent: vi.fn(),
  grantAgentBoardAccess: vi.fn(),
  listAgentsForBoard: vi.fn(),
  listMyAgents: vi.fn(),
  listPresets: vi.fn(),
  regenerateAgentKey: vi.fn(),
  revokeAgentBoardAccess: vi.fn(),
  updateAgent: vi.fn(),
  updateAgentBoardOverrides: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({
    id: 'board-1',
    name: 'Board One',
    owner_id: 'owner-1',
    agents: [],
    settings: {},
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function agent(id: string, name: string): Agent {
  return {
    id,
    name,
    description: null,
    objective: null,
    is_active: true,
    permissions: null,
    permission_flags: null,
    preset_id: null,
    created_by: 'owner-1',
    created_at: '2026-07-03T00:00:00Z',
    last_used_at: null,
  };
}

describe('AgentsModal reveal-once credentials', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listPresets.mockResolvedValue([] satisfies PermissionPreset[]);
    apiMock.listAgentsForBoard.mockResolvedValue([]);
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  it('keeps listed agents secret-free and reveals only newly created keys', async () => {
    apiMock.listMyAgents.mockResolvedValue([agent('agent-1', 'Existing Agent')]);
    apiMock.createAgent.mockResolvedValue({
      agent: agent('agent-2', 'New Agent'),
      reveal_once_secret: 'dash_new_secret',
      message: 'Copy this key now.',
    });

    render(<AgentsModal isOpen onClose={() => {}} />);

    await screen.findByText('Existing Agent');
    fireEvent.click(screen.getByText('Existing Agent'));

    expect(screen.getByText('Hidden. Regenerate to reveal a new key.')).toBeInTheDocument();
    expect(screen.queryByText('dash_new_secret')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /new agent/i }));
    fireEvent.change(screen.getByPlaceholderText('Ex: Claude Assistant'), {
      target: { value: 'New Agent' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^create agent$/i }));

    await screen.findByText('dash_new_secret');
    expect(apiMock.createAgent).toHaveBeenCalledWith({
      name: 'New Agent',
      description: undefined,
      objective: undefined,
      preset_id: undefined,
    });
  });

  it('stores regenerated keys only in local reveal state for copy/config actions', async () => {
    apiMock.listMyAgents.mockResolvedValue([agent('agent-1', 'Claude Agent')]);
    apiMock.regenerateAgentKey.mockResolvedValue({
      agent: agent('agent-1', 'Claude Agent'),
      reveal_once_secret: 'dash_rotated_secret',
      message: 'Copy this key now.',
    });

    render(<AgentsModal isOpen onClose={() => {}} />);

    await screen.findByText('Claude Agent');
    fireEvent.click(screen.getByTitle('Regenerate key'));

    await screen.findByText('dash_rotated_secret');
    fireEvent.click(screen.getByTitle('claude_desktop_config.json'));

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining('api_key=dash_rotated_secret'),
      );
    });
  });
});
