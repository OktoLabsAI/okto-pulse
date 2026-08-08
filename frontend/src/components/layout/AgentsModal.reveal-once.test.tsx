import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentsModal } from './AgentsModal';
import type { Agent, AgentSummary, PermissionPreset } from '@/types';

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
const permissionState = vi.hoisted(() => ({
  denied: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'full_control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => !permissionState.denied.has(flag),
  }),
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

function preset(
  id: string,
  name: string,
  flags: Record<string, unknown>,
): PermissionPreset {
  return {
    id,
    owner_id: null,
    name,
    description: null,
    is_builtin: true,
    base_preset_id: null,
    flags: flags as PermissionPreset['flags'],
    owner_review_required: false,
    review_reason: null,
    created_at: '2026-07-03T00:00:00Z',
    updated_at: null,
  };
}

describe('AgentsModal reveal-once credentials', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.denied = new Set();
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
    expect(screen.getByTitle('Regenerate key')).toBeEnabled();
    expect(screen.queryByText('dash_new_secret')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /new agent/i }));
    fireEvent.change(screen.getByPlaceholderText('Ex: Claude Assistant'), {
      target: { value: 'New Agent' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^create agent$/i }));

    await screen.findByText('dash_new_secret');
    fireEvent.click(screen.getByText('Existing Agent'));
    expect(screen.getByText('Hidden. Regenerate to reveal a new key.')).toBeInTheDocument();
    expect(screen.queryByText('dash_new_secret')).not.toBeInTheDocument();
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

  it('clears revealed keys on close and does not rehydrate them after reopen', async () => {
    apiMock.listMyAgents.mockResolvedValue([agent('agent-1', 'Claude Agent')]);
    apiMock.regenerateAgentKey.mockResolvedValue({
      agent: agent('agent-1', 'Claude Agent'),
      reveal_once_secret: 'dash_rotated_secret',
      message: 'Copy this key now.',
    });

    const { rerender } = render(<AgentsModal isOpen onClose={() => {}} />);

    await screen.findByText('Claude Agent');
    fireEvent.click(screen.getByTitle('Regenerate key'));
    await screen.findByText('dash_rotated_secret');

    rerender(<AgentsModal isOpen={false} onClose={() => {}} />);
    expect(screen.queryByText('dash_rotated_secret')).not.toBeInTheDocument();

    rerender(<AgentsModal isOpen onClose={() => {}} />);
    await screen.findByText('Claude Agent');
    fireEvent.click(screen.getByText('Claude Agent'));

    expect(screen.queryByText('dash_rotated_secret')).not.toBeInTheDocument();
    expect(screen.getByText('Hidden. Regenerate to reveal a new key.')).toBeInTheDocument();
  });

  it('keeps the reveal-once buffer through refetch while the modal stays open', async () => {
    apiMock.listPresets.mockResolvedValue([
      {
        id: 'preset-reviewer',
        owner_id: null,
        name: 'Reviewer',
        description: null,
        is_builtin: true,
        base_preset_id: null,
        flags: {},
        owner_review_required: false,
        review_reason: null,
        created_at: '2026-07-03T00:00:00Z',
        updated_at: null,
      } satisfies PermissionPreset,
    ]);
    apiMock.listMyAgents.mockResolvedValue([agent('agent-1', 'Claude Agent')]);
    apiMock.regenerateAgentKey.mockResolvedValue({
      agent: agent('agent-1', 'Claude Agent'),
      reveal_once_secret: 'dash_rotated_secret',
      message: 'Copy this key now.',
    });
    apiMock.updateAgent.mockResolvedValue(agent('agent-1', 'Claude Agent'));

    render(<AgentsModal isOpen onClose={() => {}} />);

    await screen.findByText('Claude Agent');
    fireEvent.click(screen.getByTitle('Regenerate key'));
    await screen.findByText('dash_rotated_secret');

    fireEvent.change(screen.getByDisplayValue('Full Control'), {
      target: { value: 'preset-reviewer' },
    });

    await waitFor(() => {
      expect(apiMock.updateAgent).toHaveBeenCalledWith('agent-1', {
        preset_id: 'preset-reviewer',
      });
      expect(apiMock.listMyAgents).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByText('dash_rotated_secret')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('claude_desktop_config.json'));

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining('api_key=dash_rotated_secret'),
      );
    });
  });

  it('edits a preset-linked effective tree and persists only its direct delta', async () => {
    const specPreset = preset(
      'preset-spec',
      'Spec',
      { board: { read: true, analytics_read: false } },
    );
    const presetAgent = {
      ...agent('agent-preset', 'Preset Agent'),
      preset_id: specPreset.id,
      permission_flags: { board: { read: false } },
    };
    apiMock.listPresets.mockResolvedValue([specPreset]);
    apiMock.listMyAgents.mockResolvedValue([presetAgent]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    fireEvent.click(await screen.findByText('Preset Agent'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit Board permissions' }),
    );
    expect(
      screen.getByRole('button', { name: 'Toggle board.read' }),
    ).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Toggle board.read' }));

    await waitFor(() => {
      expect(apiMock.updateAgent).toHaveBeenCalledWith('agent-preset', {
        permission_flags: {},
      });
    });
  });

  it('edits Full Control from its real preset base and stores a sparse delta', async () => {
    const fullControl = preset(
      'preset-full',
      'Full Control',
      { board: { read: true, analytics_read: true } },
    );
    apiMock.listPresets.mockResolvedValue([fullControl]);
    apiMock.listMyAgents.mockResolvedValue([
      agent('agent-full', 'Full Control Agent'),
    ]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    fireEvent.click(await screen.findByText('Full Control Agent'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit Board permissions' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Toggle board.read' }));

    await waitFor(() => {
      expect(apiMock.updateAgent).toHaveBeenCalledWith('agent-full', {
        permission_flags: { board: { read: false } },
      });
    });
  });

  it('resolves a preset-less delta from Full Control when built-ins are shuffled', async () => {
    const executor = preset(
      'preset-executor',
      'Executor',
      { board: { read: false, analytics_read: false } },
    );
    const fullControl = preset(
      'preset-full',
      'Full Control',
      { board: { read: true, analytics_read: true } },
    );
    const fullAgent = {
      ...agent('agent-shuffled', 'Shuffled Full Agent'),
      permission_flags: { board: { read: false } },
    };
    apiMock.listPresets.mockResolvedValue([executor, fullControl]);
    apiMock.listMyAgents.mockResolvedValue([fullAgent]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    fireEvent.click(await screen.findByText('Shuffled Full Agent'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit Board permissions' }),
    );

    expect(screen.getByTestId('permission-diff-base')).toHaveTextContent(
      'Base: Full Control',
    );
    expect(
      screen.getByRole('button', { name: 'Toggle board.read' }),
    ).toHaveAttribute('aria-pressed', 'false');
    expect(
      screen.getByRole('button', { name: 'Toggle board.analytics_read' }),
    ).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(
      screen.getByRole('button', { name: 'Toggle board.analytics_read' }),
    );
    await waitFor(() => {
      expect(apiMock.updateAgent).toHaveBeenCalledWith('agent-shuffled', {
        permission_flags: {
          board: { read: false, analytics_read: false },
        },
      });
    });
  });

  it('keeps selected preset owner-review visible while using its fail-closed base', async () => {
    const dangerous = {
      ...preset(
        'preset-danger',
        'Dangling Custom',
        { board: { read: false, analytics_read: false } },
      ),
      is_builtin: false,
      base_preset_id: 'missing-base',
      owner_review_required: true,
      review_reason: 'dangling_base_preset',
    } satisfies PermissionPreset;
    const presetAgent = {
      ...agent('agent-danger', 'Danger Agent'),
      preset_id: dangerous.id,
      permission_flags: {},
    };
    apiMock.listPresets.mockResolvedValue([
      preset(
        'preset-full',
        'Full Control',
        { board: { read: true, analytics_read: true } },
      ),
      dangerous,
    ]);
    apiMock.listMyAgents.mockResolvedValue([presetAgent]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    fireEvent.click(await screen.findByText('Danger Agent'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Owner review required for Dangling Custom · dangling_base_preset',
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit Board permissions' }),
    );
    expect(
      screen.getByRole('button', { name: 'Toggle board.read' }),
    ).toHaveAttribute('aria-pressed', 'false');
  });

  it('round-trips the projected raw board ceiling against the real agent base', async () => {
    const fullControl = preset(
      'preset-full',
      'Full Control',
      { board: { read: true, analytics_read: true } },
    );
    const fullAgent = agent('agent-board', 'Board Agent');
    const boardAgent: AgentSummary = {
      id: fullAgent.id,
      name: fullAgent.name,
      description: null,
      objective: null,
      is_active: true,
      preset_id: null,
      permission_flags: null,
      permission_overrides: { board: { read: false } },
      created_at: fullAgent.created_at,
      last_used_at: null,
    };
    apiMock.listPresets.mockResolvedValue([fullControl]);
    apiMock.listMyAgents.mockResolvedValue([fullAgent]);
    apiMock.listAgentsForBoard.mockResolvedValue([boardAgent]);
    apiMock.updateAgentBoardOverrides.mockResolvedValue({
      id: 'grant-1',
      agent_id: fullAgent.id,
      board_id: 'board-1',
      granted_by: 'owner-1',
      granted_at: '2026-07-03T00:00:00Z',
      permission_overrides: null,
    });

    render(<AgentsModal isOpen onClose={() => {}} />);
    await screen.findByText('Board Agent');
    fireEvent.click(screen.getByRole('button', { name: /board access/i }));
    await waitFor(() => {
      expect(apiMock.listAgentsForBoard).toHaveBeenCalledWith('board-1');
    });
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Edit board access for Board Agent',
      }),
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit Board permissions' }),
    );

    const readToggle = screen.getByRole('button', {
      name: 'Toggle board.read',
    });
    expect(readToggle).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(readToggle);

    await waitFor(() => {
      expect(apiMock.updateAgentBoardOverrides).toHaveBeenCalledWith(
        'agent-board',
        'board-1',
        null,
      );
    });
  });

  it('does not issue agent reads when the exact read leaves are denied', async () => {
    permissionState.denied = new Set([
      'agent.entity.read',
      'agent.board_access.read',
      'permission_preset.entity.read',
    ]);
    apiMock.listMyAgents.mockResolvedValue([]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    expect(await screen.findByText('You do not have permission to view agents')).toBeInTheDocument();
    expect(apiMock.listMyAgents).not.toHaveBeenCalled();
    expect(apiMock.listPresets).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /board access/i }));
    expect(await screen.findByText('You do not have permission to view board access')).toBeInTheDocument();
    expect(apiMock.listAgentsForBoard).not.toHaveBeenCalled();
  });

  it('requires agent.api_key.rotate before invoking key rotation', async () => {
    permissionState.denied = new Set(['agent.api_key.rotate']);
    apiMock.listMyAgents.mockResolvedValue([agent('agent-locked', 'Locked Agent')]);

    render(<AgentsModal isOpen onClose={() => {}} />);

    await screen.findByText('Locked Agent');
    const rotate = screen.getByTitle('Regenerate key');
    expect(rotate).toBeDisabled();
    fireEvent.click(rotate);
    expect(apiMock.regenerateAgentKey).not.toHaveBeenCalled();
  });
});
