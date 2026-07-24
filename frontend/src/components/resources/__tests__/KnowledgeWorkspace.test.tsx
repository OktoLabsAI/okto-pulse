import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KnowledgeWorkspace } from '../KnowledgeWorkspace';
import type { EffectiveResourcesResponse, KnowledgeWorkspaceItem } from '@/types';

const apiMock = vi.hoisted(() => ({
  getEffectiveResources: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

vi.mock('@/components/shared/MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));

function workspaceItem(overrides: Partial<KnowledgeWorkspaceItem> = {}): KnowledgeWorkspaceItem {
  return {
    resource_type: 'knowledge_base',
    canonical_unique_resource_id: 'knowledge_base:root-1',
    versioned_projection_id: 'knowledge_base:root-1@7',
    root_id: 'root-1',
    resource_version: '7',
    representative_resource_id: 'kb-1',
    title: 'Runbook',
    attachment_kind: 'inherited_reference',
    inherited: true,
    grandfathered: false,
    stale: true,
    superseded: false,
    provenance: {
      source_entity_type: 'spec',
      source_entity_id: 'spec-1',
      source_entity_title: 'Parent spec',
      origin_class: 'v2',
      source_revision: '7',
      source_content_sha256: 'abc',
    },
    physical_attachments: [
      {
        resource_id: 'kb-1',
        attachment_kind: 'inherited_reference',
        inherited: true,
        source_entity_type: 'spec',
        source_entity_id: 'spec-1',
        source_entity_title: 'Parent spec',
        effective: true,
        resource_version: '7',
        revision_stamp: { source_revision: 7 },
      },
      {
        resource_id: 'kb-copy',
        attachment_kind: 'direct',
        inherited: false,
        source_entity_type: 'card',
        source_entity_id: 'card-1',
        source_entity_title: 'Task',
        effective: true,
        resource_version: '7',
        revision_stamp: { source_revision: 7 },
      },
    ],
    detail_cursor: 'opaque-detail-1',
    relevance_links: [{
      entity_type: 'functional_requirement',
      entity_id: 'fr_123',
    }],
    body_omitted_reason: 'profile_summary',
    ...overrides,
  };
}

function page(
  items: KnowledgeWorkspaceItem[],
  overrides: Partial<EffectiveResourcesResponse> = {},
): EffectiveResourcesResponse {
  return {
    contract_version: 2,
    board_id: 'board-1',
    entity_type: 'card',
    entity_id: 'card-1',
    profile: 'summary',
    items,
    count: items.length,
    total_count: items.length,
    next_cursor: null,
    truncated: false,
    unique_effective_count: 1,
    raw_attachment_count: 2,
    workspace_item_count: 1,
    unique_root_version_count: 1,
    response_bytes: 900,
    resources: { architecture: [], mockup: [], knowledge_base: [] },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe('KnowledgeWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses summary pagination and hydrates exactly one detail lazily', async () => {
    const first = workspaceItem();
    const second = workspaceItem({
      canonical_unique_resource_id: 'knowledge_base:root-2',
      versioned_projection_id: 'knowledge_base:root-2@legacy',
      root_id: 'root-2',
      resource_version: null,
      representative_resource_id: 'kb-2',
      title: 'Historical notes',
      inherited: false,
      grandfathered: true,
      stale: false,
      detail_cursor: 'opaque-detail-2',
    });
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { profile: string; cursor?: string },
      ) => {
        if (options.profile === 'detail') {
          return page([
            {
              ...first,
              body: { content: '# Operational detail' },
              body_omitted_reason: undefined,
            },
          ], { profile: 'detail' });
        }
        if (options.cursor === 'opaque-next') {
          return page([second], {
            total_count: 2,
            workspace_item_count: 2,
            unique_root_version_count: 2,
          });
        }
        return page([first], {
          total_count: 2,
          next_cursor: 'opaque-next',
          truncated: true,
          workspace_item_count: 2,
          unique_root_version_count: 2,
        });
      },
    );

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );

    expect(await screen.findByText('Runbook')).toBeInTheDocument();
    expect(screen.getByText('Logical roots').nextElementSibling).toHaveTextContent('1');
    expect(screen.getByText('Physical links').nextElementSibling).toHaveTextContent('2');
    expect(screen.getByText('Workspace rows').nextElementSibling).toHaveTextContent('2');
    expect(screen.getByText('inherited')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.queryByText('# Operational detail')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Runbook'));
    expect(await screen.findByText('# Operational detail')).toBeInTheDocument();
    expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
      'board-1',
      'card',
      'card-1',
      { profile: 'detail', cursor: 'opaque-detail-1' },
    );
    expect(screen.getByText('functional requirement: fr_123')).toBeInTheDocument();
    expect(screen.getByText('2 physical attachment(s)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('Historical notes')).toBeInTheDocument();
    expect(screen.getByText('grandfathered')).toBeInTheDocument();
    expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
      'board-1',
      'card',
      'card-1',
      { profile: 'summary', cursor: 'opaque-next', limit: 25 },
    );
  });

  it('normalizes the explicit legacy envelope during rolling upgrades', async () => {
    apiMock.getEffectiveResources.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'spec',
      entity_id: 'spec-1',
      profile: 'legacy',
      resources: {
        architecture: [],
        mockup: [],
        knowledge_base: [
          {
            id: 'legacy-kb',
            title: 'Legacy reference',
            resource_type: 'knowledge_base',
            attachment_kind: 'direct',
            inherited: false,
            read_only: false,
            hydrated: true,
            resource: { id: 'legacy-kb', title: 'Legacy reference', content: 'Legacy body' },
          },
        ],
      },
    });

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
      />,
    );

    expect(await screen.findByText('Legacy reference')).toBeInTheDocument();
    expect(screen.getByText('grandfathered')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Legacy reference'));
    expect(await screen.findByText('Legacy body')).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(1));
  });

  it('rejects detail that does not match the requested projection', async () => {
    const requested = workspaceItem();
    const wrong = workspaceItem({
      canonical_unique_resource_id: 'knowledge_base:other',
      versioned_projection_id: 'knowledge_base:other@9',
      root_id: 'other',
      resource_version: '9',
      representative_resource_id: 'kb-other',
      title: 'Wrong detail',
      body: { content: 'must not leak' },
    });
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { profile: string },
      ) => (
        options.profile === 'detail'
          ? page([wrong], { profile: 'detail' })
          : page([requested])
      ),
    );

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );

    fireEvent.click(await screen.findByText('Runbook'));
    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Knowledge detail response did not match the requested resource.',
      );
    });
    expect(screen.queryByText('must not leak')).not.toBeInTheDocument();
  });

  it('ignores an earlier entity response that settles after a rerender', async () => {
    const firstRequest = deferred<EffectiveResourcesResponse>();
    const secondRequest = deferred<EffectiveResourcesResponse>();
    apiMock.getEffectiveResources.mockImplementation(
      (
        _boardId: string,
        _entityType: string,
        entityId: string,
      ) => (entityId === 'card-1' ? firstRequest.promise : secondRequest.promise),
    );

    const { rerender } = render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );
    await waitFor(() => expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(1));

    rerender(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-2"
      />,
    );
    await waitFor(() => expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(2));

    act(() => {
      secondRequest.resolve(page([
        workspaceItem({
          versioned_projection_id: 'knowledge_base:new@1',
          canonical_unique_resource_id: 'knowledge_base:new',
          root_id: 'new',
          representative_resource_id: 'kb-new',
          title: 'New entity knowledge',
        }),
      ], { entity_id: 'card-2' }));
    });
    expect(await screen.findByText('New entity knowledge')).toBeInTheDocument();

    act(() => {
      firstRequest.resolve(page([
        workspaceItem({ title: 'Stale entity knowledge' }),
      ]));
    });
    await waitFor(() => {
      expect(screen.queryByText('Stale entity knowledge')).not.toBeInTheDocument();
    });
    expect(screen.getByText('New entity knowledge')).toBeInTheDocument();
  });

  it('deduplicates overlapping pages and stops a repeated cursor loop', async () => {
    const first = workspaceItem();
    const second = workspaceItem({
      canonical_unique_resource_id: 'knowledge_base:root-2',
      versioned_projection_id: 'knowledge_base:root-2@1',
      root_id: 'root-2',
      resource_version: '1',
      representative_resource_id: 'kb-2',
      title: 'Second page knowledge',
    });
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { cursor?: string },
      ) => (
        options.cursor
          ? page([first, second], {
            total_count: 2,
            next_cursor: 'same-cursor',
            truncated: true,
          })
          : page([first], {
            total_count: 2,
            next_cursor: 'same-cursor',
            truncated: true,
          })
      ),
    );

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('Second page knowledge')).toBeInTheDocument();
    expect(screen.getAllByText('Runbook')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
    expect(toastMock.error).toHaveBeenCalledWith(
      'Knowledge Workspace pagination stopped after a repeated cursor.',
    );
  });

  it('exposes an omitted body reference as a copy action', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const first = workspaceItem();
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { profile: string },
      ) => (
        options.profile === 'detail'
          ? page([{
            ...first,
            body_omitted_reason: 'body_size_limit',
            body_ref: {
              resource_type: 'knowledge_base',
              resource_id: 'kb-1',
            },
          }], { profile: 'detail' })
          : page([first])
      ),
    );

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );

    fireEvent.click(await screen.findByText('Runbook'));
    fireEvent.click(await screen.findByRole('button', { name: 'Copy source reference' }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith('knowledge_base:kb-1');
    });
    expect(toastMock.success).toHaveBeenCalledWith(
      'Knowledge source reference copied.',
    );
  });

  it('includes the resource version in a downloaded filename', async () => {
    const first = workspaceItem();
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { profile: string },
      ) => (
        options.profile === 'detail'
          ? page([{
            ...first,
            body: { content: 'downloaded detail' },
            body_omitted_reason: undefined,
          }], { profile: 'detail' })
          : page([first])
      ),
    );
    const createObjectURL = vi.fn().mockReturnValue('blob:workspace-download');
    const revokeObjectURL = vi.fn();
    (URL as typeof URL & { createObjectURL: typeof createObjectURL }).createObjectURL = createObjectURL;
    (URL as typeof URL & { revokeObjectURL: typeof revokeObjectURL }).revokeObjectURL = revokeObjectURL;
    let downloadedFilename = '';
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function captureDownload(this: HTMLAnchorElement) {
        downloadedFilename = this.download;
      });

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Download Runbook' }));
    await waitFor(() => {
      expect(downloadedFilename).toBe('Runbook_v7.md');
    });
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:workspace-download');
    clickSpy.mockRestore();
  });

  it('does not reload when the delete callback reports cancellation', async () => {
    const direct = workspaceItem({ inherited: false });
    const onDelete = vi.fn().mockResolvedValue(false);
    apiMock.getEffectiveResources.mockResolvedValue(page([direct]));

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
        onDelete={onDelete}
      />,
    );

    const deleteButton = await screen.findByRole('button', { name: 'Delete Runbook' });
    fireEvent.click(deleteButton);
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith('kb-1'));
    await waitFor(() => expect(deleteButton).toBeEnabled());
    expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(1);
  });

  it('reloads exactly once after a successful delete', async () => {
    const direct = workspaceItem({ inherited: false });
    const onDelete = vi.fn().mockResolvedValue(true);
    apiMock.getEffectiveResources
      .mockResolvedValueOnce(page([direct]))
      .mockResolvedValueOnce(page([]));

    render(
      <KnowledgeWorkspace
        boardId="board-1"
        entityType="card"
        entityId="card-1"
        onDelete={onDelete}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Runbook' }));
    expect(await screen.findByText('No knowledge bases')).toBeInTheDocument();
    expect(onDelete).toHaveBeenCalledWith('kb-1');
    expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(2);
  });
});
