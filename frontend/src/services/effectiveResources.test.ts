import { describe, expect, it, vi } from 'vitest';
import { loadEffectiveResourceItems } from './effectiveResources';
import { workspaceItem, workspacePage } from '@/testFixtures/effectiveResources';

describe('current effective resource pagination', () => {
  it('loads every inherited artifact page with complete-body profile', async () => {
    const first = workspaceItem('mockup', 'one', 'First');
    const second = workspaceItem('mockup', 'two', 'Second', { inherited: true });
    const read = vi.fn()
      .mockResolvedValueOnce(workspacePage('b', 'spec', 's', 'mockup', [first], 'full', { next_cursor: 'next' }))
      .mockResolvedValueOnce(workspacePage('b', 'spec', 's', 'mockup', [second], 'full'));
    expect(await loadEffectiveResourceItems(read, 'b', 'spec', 's', 'mockup', 'full')).toEqual([first, second]);
    expect(read).toHaveBeenLastCalledWith('b', 'spec', 's', {
      resource_type: 'mockup', profile: 'full', limit: 1, cursor: 'next',
    });
  });

  it('rejects a page belonging to a different resource kind', async () => {
    const read = vi.fn().mockResolvedValue(workspacePage('b', 'spec', 's', 'architecture'));
    await expect(loadEffectiveResourceItems(read, 'b', 'spec', 's', 'mockup')).rejects.toThrow('different scope');
  });

  it('stops repeated cursors instead of looping or duplicating the inventory', async () => {
    const read = vi.fn().mockResolvedValue(workspacePage('b', 'spec', 's', 'knowledge_base', [], 'summary', { next_cursor: 'same' }));
    await expect(loadEffectiveResourceItems(read, 'b', 'spec', 's', 'knowledge_base')).rejects.toThrow('repeated cursor');
    expect(read).toHaveBeenCalledTimes(2);
  });
});
