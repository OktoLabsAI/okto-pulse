import type { EffectiveResourceItem } from '@/types';

export interface KnowledgePropagationCandidate {
  id: string;
  title: string;
  description?: string | null;
  stale?: boolean;
  origin_class?: string | null;
}

interface PhysicalKnowledgeCandidate {
  id: string;
  title: string;
  description?: string | null;
  root_source_kb_id?: string | null;
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

export function physicalKnowledgeCandidate(
  item: PhysicalKnowledgeCandidate,
): KnowledgePropagationCandidate {
  return {
    id: item.root_source_kb_id || item.id,
    title: item.title,
    description: item.description,
    stale: false,
    origin_class: null,
  };
}

export function effectiveKnowledgeCandidate(
  item: EffectiveResourceItem,
): KnowledgePropagationCandidate | null {
  const resource =
    item.resource && typeof item.resource === 'object'
      ? item.resource as Record<string, unknown>
      : {};
  const id =
    optionalString(item.ref?.root_resource_id)
    ?? optionalString(resource.root_source_kb_id)
    ?? optionalString(item.resource_id)
    ?? optionalString(resource.id)
    ?? optionalString(item.id);
  if (!id) return null;

  return {
    id,
    title:
      optionalString(resource.title)
      ?? optionalString(item.title)
      ?? 'Knowledge resource',
    description: optionalString(resource.description),
    stale: Boolean(item.ref?.knowledge_assignment_stale),
    origin_class: optionalString(item.ref?.origin_class),
  };
}

export function mergeKnowledgePropagationCandidates(
  ...groups: KnowledgePropagationCandidate[][]
): KnowledgePropagationCandidate[] {
  const merged = new Map<string, KnowledgePropagationCandidate>();
  groups.flat().forEach((candidate) => {
    const current = merged.get(candidate.id);
    merged.set(candidate.id, {
      id: candidate.id,
      title:
        candidate.title !== 'Knowledge resource'
          ? candidate.title
          : current?.title ?? candidate.title,
      description: candidate.description ?? current?.description,
      stale: candidate.stale ?? current?.stale,
      origin_class: candidate.origin_class ?? current?.origin_class,
    });
  });
  return Array.from(merged.values()).sort((left, right) =>
    left.title.localeCompare(right.title),
  );
}
