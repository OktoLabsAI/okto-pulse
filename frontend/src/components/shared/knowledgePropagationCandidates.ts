import type { KnowledgeWorkspaceItem } from '@/types';

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

export function workspaceKnowledgeCandidate(
  item: KnowledgeWorkspaceItem,
): KnowledgePropagationCandidate {
  return {
    id: item.root_id,
    title: item.title || item.root_id,
    stale: item.stale,
    origin_class: item.provenance.origin_class,
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
