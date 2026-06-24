/**
 * CardKnowledgeTab - read-only Knowledge Base snapshots for a card/task.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, ChevronDown, ChevronUp, Download, Shield } from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import type { Card, EffectiveResourceItem } from '@/types';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

interface CardKnowledgeTabProps {
  card: Card;
  specKnowledgeBases: { id: string; title: string; description?: string; content: string; mime_type?: string }[];
  onUpdate: (kbs: any[]) => Promise<void>;
}

function isSpecSnapshot(kb: any): boolean {
  const source = String(kb.source || '');
  return source === 'spec' || source.startsWith('copied_from_spec:') || Boolean(kb.source_kb_id);
}

function effectiveKnowledgeToCardSnapshot(item: EffectiveResourceItem): any | null {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Record<string, unknown>
    : item as Record<string, unknown>;
  const id = String(item.id || resource.id || '');
  if (!id) return null;
  return {
    id,
    title: String(resource.title || item.title || 'Inherited knowledge'),
    description: typeof resource.description === 'string' ? resource.description : null,
    content: typeof resource.content === 'string' ? resource.content : '',
    mime_type: typeof resource.mime_type === 'string' ? resource.mime_type : 'text/markdown',
    inherited: item.inherited,
    read_only: item.read_only,
    source_entity_type: item.source_entity_type ?? item.provenance?.source_entity_type ?? null,
    source_entity_id: item.source_entity_id ?? item.provenance?.source_entity_id ?? null,
    source_entity_title: item.source_entity_title ?? item.provenance?.source_entity_title ?? null,
  };
}

function sourceLabel(kb: any): string {
  const type = kb.source_entity_type || 'source';
  const title = kb.source_entity_title || kb.source_entity_id || 'parent';
  return `${type}: ${title}`;
}

export function CardKnowledgeTab({ card }: CardKnowledgeTabProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const [effectiveItems, setEffectiveItems] = useState<EffectiveResourceItem[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const cardKBs: any[] = useMemo(() => {
    const direct = card.knowledge_bases || [];
    const directIds = new Set(direct.map((item: any) => item.id));
    const inherited = effectiveItems
      .filter((item) => item.inherited && !directIds.has(item.id))
      .map(effectiveKnowledgeToCardSnapshot)
      .filter(Boolean);
    return [...direct, ...inherited];
  }, [card.knowledge_bases, effectiveItems]);

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    apiRef.current.getEffectiveResources(card.board_id, 'card', card.id)
      .then((response) => {
        if (!cancelled) setEffectiveItems(response.resources.knowledge_base || []);
      })
      .catch(() => {
        if (!cancelled) setEffectiveItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [card.board_id, card.id]);

  const downloadMarkdown = (kb: any) => {
    const safeTitle = (kb.title || 'knowledge').replace(/[^A-Za-z0-9._-]+/g, '_');
    const filename = `${safeTitle || 'knowledge'}.md`;
    const body = `# ${kb.title || ''}\n\n> ${kb.description || ''}\n\n${kb.content || ''}\n`;
    const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="modal-body space-y-4">
      <div className="px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 text-sm text-gray-600 dark:text-gray-300 flex items-center gap-2">
        <Shield size={15} />
        Card knowledge snapshots are read-only
      </div>

      {cardKBs.length === 0 ? (
        <div className="text-center py-8">
          <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge bases</p>
          <p className="text-xs text-gray-400 mt-1">Copy knowledge from the parent spec to populate card context.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {cardKBs.map((kb: any) => (
            <div key={kb.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
              <div
                data-testid={`kb-row-${kb.id}`}
                className="flex items-center gap-2 p-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                onClick={() => setExpandedId(expandedId === kb.id ? null : kb.id)}
              >
                <BookOpen size={14} className="text-gray-400 shrink-0" />
                <span className="text-sm font-medium text-gray-800 dark:text-gray-200 flex-1 truncate">{kb.title}</span>
                {isSpecSnapshot(kb) && (
                  <span className="text-[9px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 rounded">from spec</span>
                )}
                {kb.inherited && (
                  <span className="text-[9px] px-1.5 py-0.5 bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-200 rounded">
                    from {sourceLabel(kb)}
                  </span>
                )}
                <span className="text-[9px] text-gray-400">{kb.mime_type || 'text/markdown'}</span>
                <button
                  onClick={(e) => { e.stopPropagation(); downloadMarkdown(kb); }}
                  className="text-gray-400 hover:text-emerald-600 p-0.5"
                  aria-label="Download markdown"
                  data-testid={`kb-download-${kb.id}`}
                >
                  <Download size={12} />
                </button>
                {expandedId === kb.id ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
              </div>
              {expandedId === kb.id && (
                <div className="px-3 pb-3 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/30">
                  <div className="pt-2 text-sm prose dark:prose-invert max-w-none">
                    <MarkdownContent content={kb.content} />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
