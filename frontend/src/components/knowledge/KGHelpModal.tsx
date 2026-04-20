/**
 * KGHelpModal — in-app help for the Knowledge Graph screen.
 *
 * Mirrors the structure of HelpPanel (centered modal, left-side index,
 * markdown body, ESC / click-outside / X to close). Content lives in
 * KGHelpContent.tsx as a static TS array — no API, no translations.
 *
 * Props shape is preserved from the previous stub so existing callers
 * keep working.
 */

import { useEffect, useState } from 'react';
import { ChevronRight, Network, X } from 'lucide-react';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { KG_HELP_SECTIONS } from './KGHelpContent';
import { SCHEMA_VERSION } from '@/constants/kg';

interface KGHelpModalProps {
  onClose: () => void;
  /** Optional section id to open on first render (deep-link). */
  initialSectionId?: string;
}

export function KGHelpModal({ onClose, initialSectionId }: KGHelpModalProps) {
  const defaultSection =
    KG_HELP_SECTIONS.find((s) => s.id === initialSectionId)?.id ??
    KG_HELP_SECTIONS[0].id;
  const [activeSection, setActiveSection] = useState(defaultSection);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const current =
    KG_HELP_SECTIONS.find((s) => s.id === activeSection) ?? KG_HELP_SECTIONS[0];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
      data-testid="kg-help-modal-overlay"
    >
      <div
        className="relative w-[90vw] max-w-5xl bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 flex overflow-hidden"
        style={{ height: '85vh' }}
        onClick={(e) => e.stopPropagation()}
        data-testid="kg-help-modal"
      >
        {/* Left sidebar — section index, same pattern as HelpPanel */}
        <nav className="w-56 shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 flex flex-col">
          <div className="px-4 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2.5">
            <Network size={20} className="text-blue-500" />
            <div>
              <h2 className="text-sm font-bold text-gray-800 dark:text-gray-200">
                Knowledge Graph Help
              </h2>
              <p className="text-[10px] text-gray-400 mt-0.5">
                Schema version: {SCHEMA_VERSION}
              </p>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto py-2">
            {KG_HELP_SECTIONS.map((section) => (
              <button
                key={section.id}
                onClick={() => setActiveSection(section.id)}
                data-testid={`kg-help-nav-${section.id}`}
                className={`w-full text-left px-4 py-2 text-sm flex items-center gap-2.5 transition-colors ${
                  activeSection === section.id
                    ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 font-medium border-r-2 border-blue-500'
                    : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700/50'
                }`}
              >
                <span className="shrink-0 opacity-70">{section.icon}</span>
                {section.title}
                {activeSection === section.id && (
                  <ChevronRight size={12} className="ml-auto opacity-50" />
                )}
              </button>
            ))}
          </div>
        </nav>

        {/* Content area */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="flex items-center justify-between px-6 py-3 border-b border-gray-200 dark:border-gray-700">
            <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
              {current.icon}
              {current.title}
            </h3>
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              aria-label="Close help"
            >
              <X size={18} />
            </button>
          </div>
          <div
            className="flex-1 overflow-y-auto px-6 py-4"
            data-testid={`kg-help-content-${current.id}`}
          >
            {current.body.kind === 'markdown' ? (
              <MarkdownContent content={current.body.text} />
            ) : (
              current.body.node
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
