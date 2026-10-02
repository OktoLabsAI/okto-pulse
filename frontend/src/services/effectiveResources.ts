import type {
  EffectiveResourcesOptions, EffectiveResourcesResponse, KnowledgeWorkspaceItem,
  ResourceGateEntityType, ResourceGateResourceType,
} from '@/types';

type ReadPage = (
  boardId: string, entityType: ResourceGateEntityType, entityId: string,
  options?: EffectiveResourcesOptions,
) => Promise<EffectiveResourcesResponse>;

/** Collect current pages without losing inherited resources after the first page. */
export async function loadEffectiveResourceItems(
  readPage: ReadPage, boardId: string, entityType: ResourceGateEntityType,
  entityId: string, resourceType: ResourceGateResourceType,
  profile: 'summary' | 'full' = 'summary',
): Promise<KnowledgeWorkspaceItem[]> {
  const items: KnowledgeWorkspaceItem[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await readPage(boardId, entityType, entityId, {
      profile, resource_type: resourceType,
      limit: resourceType !== 'knowledge_base' && profile === 'full' ? 1 : 25,
      ...(cursor ? { cursor } : {}),
    });
    if (page.board_id !== boardId || page.entity_type !== entityType
      || page.entity_id !== entityId || page.resource_type !== resourceType) {
      throw new Error('Effective resource page belongs to a different scope.');
    }
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor) {
      if (seen.has(cursor)) throw new Error('Effective resource pagination returned a repeated cursor.');
      seen.add(cursor);
    }
  } while (cursor);
  return items;
}
