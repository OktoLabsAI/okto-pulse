/**
 * MockupsTab — Renders screen mockups as HTML iframes with Tailwind CDN.
 */

import { useState } from 'react';
import { Monitor, Smartphone, MessageSquare } from 'lucide-react';
import type { ScreenMockup } from '@/types';

function sanitizeHtml(html: string): string {
  // Strip <script> tags and on* event handlers
  let clean = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
  clean = clean.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  return clean;
}

export function MockupsTab({
  screenMockups,
  expanded = false,
}: {
  screenMockups: ScreenMockup[] | null;
  expanded?: boolean;
}) {
  const screens = (screenMockups || []).sort((a, b) => a.order - b.order);
  const [selectedId, setSelectedId] = useState<string>(screens[0]?.id || '');
  const [viewMode, setViewMode] = useState<'desktop' | 'mobile'>('desktop');
  const selected = screens.find((s) => s.id === selectedId);

  if (screens.length === 0) {
    return (
      <div className="text-center py-12">
        <Monitor size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
        <p className="text-sm text-gray-500 dark:text-gray-400">No screen mockups yet</p>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
          Screen mockups can be added via the API
        </p>
      </div>
    );
  }

  const sanitizedHtml = selected ? sanitizeHtml(selected.html_content) : '';
  const srcDoc = `<!DOCTYPE html><html><head><script src="https://cdn.tailwindcss.com"><\/script></head><body class="p-4 bg-white">${sanitizedHtml}</body></html>`;

  return (
    <div className="space-y-3">
      {/* Screen selector + controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1 flex-wrap">
          {screens.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelectedId(s.id)}
              className={`px-2.5 py-1 rounded text-xs flex items-center gap-1.5 transition-colors ${
                s.id === selectedId
                  ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 font-medium'
                  : 'bg-gray-100 dark:bg-gray-800 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
              }`}
            >
              {s.title}
              <span className="text-[9px] text-gray-400">{s.screen_type}</span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1 border border-gray-200 dark:border-gray-700 rounded-lg p-0.5">
          <button
            onClick={() => setViewMode('desktop')}
            className={`p-1 rounded ${viewMode === 'desktop' ? 'bg-gray-200 dark:bg-gray-700' : ''}`}
            title="Desktop view"
          >
            <Monitor size={14} className={viewMode === 'desktop' ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400'} />
          </button>
          <button
            onClick={() => setViewMode('mobile')}
            className={`p-1 rounded ${viewMode === 'mobile' ? 'bg-gray-200 dark:bg-gray-700' : ''}`}
            title="Mobile view"
          >
            <Smartphone size={14} className={viewMode === 'mobile' ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400'} />
          </button>
        </div>
      </div>

      {/* Iframe viewport */}
      {selected && (
        <div className={`mx-auto transition-all ${viewMode === 'mobile' ? 'max-w-sm' : 'w-full'}`}>
          {selected.description && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2 italic">{selected.description}</p>
          )}
          <iframe
            srcDoc={srcDoc}
            sandbox="allow-same-origin allow-scripts"
            className="w-full border border-gray-200 dark:border-gray-700 rounded-lg"
            style={{ height: expanded ? '70vh' : '400px' }}
            title={selected.title}
          />
        </div>
      )}

      {/* Annotations */}
      {selected?.annotations && selected.annotations.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Annotations</p>
          {selected.annotations.map((a) => (
            <div key={a.id} className="flex items-start gap-1.5 text-[10px] text-gray-500 dark:text-gray-400">
              <MessageSquare size={10} className="shrink-0 mt-0.5 text-amber-400" />
              <span>{a.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
