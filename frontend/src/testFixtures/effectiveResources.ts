import type {
  EffectiveResourcesResponse, KnowledgeWorkspaceItem, KnowledgeWorkspaceProfile,
  ResourceGateEntityType, ResourceGateResourceType,
} from '@/types';

export function workspaceItem(
  kind: ResourceGateResourceType, id: string, title: string,
  overrides: Partial<KnowledgeWorkspaceItem> = {},
): KnowledgeWorkspaceItem {
  return {
    resource_type: kind, root_id: id, representative_resource_id: id, title,
    canonical_unique_resource_id: `${kind}:${id}`,
    versioned_projection_id: `${kind}:${id}@1`, resource_version: '1',
    attachment_kind: 'direct', inherited: false,
    stale: false, superseded: false, physical_attachments: [], relevance_links: [],
    detail_cursor: `detail-${id}`, provenance: {
      source_entity_type: null, source_entity_id: null, source_entity_title: null,
      origin_class: 'v2', source_revision: '1', source_content_sha256: null,
    }, ...overrides,
  };
}

export function workspacePage(
  boardId: string, entityType: ResourceGateEntityType, entityId: string,
  kind: ResourceGateResourceType = 'knowledge_base', items: KnowledgeWorkspaceItem[] = [],
  profile: KnowledgeWorkspaceProfile = 'summary',
  overrides: Partial<EffectiveResourcesResponse> = {},
): EffectiveResourcesResponse {
  return {
    contract_version: 2, board_id: boardId, entity_type: entityType, entity_id: entityId,
    resource_type: kind, profile, items, count: items.length, total_count: items.length,
    next_cursor: null, truncated: false, unique_effective_count: items.length,
    raw_attachment_count: items.length, workspace_item_count: items.length,
    unique_root_version_count: items.length, response_bytes: 0, ...overrides,
  };
}
