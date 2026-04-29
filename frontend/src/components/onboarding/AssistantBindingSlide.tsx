/**
 * Slide 3 — copy the MCP URL, paste it into the user's coding agent. The
 * Copy button writes to the clipboard and surfaces a transient "Copied!"
 * affordance plus a polite aria-live announcement.
 */

import { useEffect, useRef, useState } from 'react';

export const ASSISTANT_BINDING_SLIDE_TITLE_ID = 'onboarding-slide-3-title';

const CLIENTS = ['Claude Code', 'Cursor', 'Windsurf', 'VS Code', 'Cline'] as const;

interface AssistantBindingSlideProps {
  /**
   * The agent's MCP URL the user should paste into their coding agent.
   * When omitted, a documentation placeholder is shown so the slide
   * still renders cleanly in tests/storybook contexts.
   */
  mcpUrl?: string;
  onCopySuccess?: () => void;
  onCopyError?: () => void;
}

const FALLBACK_URL = 'http://127.0.0.1:8101/mcp?api_key=dash_…';

export function AssistantBindingSlide({
  mcpUrl,
  onCopySuccess,
  onCopyError,
}: AssistantBindingSlideProps) {
  const url = mcpUrl ?? FALLBACK_URL;
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      onCopySuccess?.();
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      onCopyError?.();
    }
  };

  return (
    <div>
      <div className="mono text-[10px] uppercase tracking-[0.2em] text-gray-400 dark:text-gray-500 mb-3.5">
        03 &middot; Assistant binding
      </div>
      <h2
        id={ASSISTANT_BINDING_SLIDE_TITLE_ID}
        className="text-2xl font-semibold tracking-tight text-gray-900 dark:text-white mb-4"
      >
        Connect your <span className="onboarding-accent">coding agent</span>
      </h2>
      <p className="text-[13.5px] leading-snug text-gray-500 dark:text-gray-400 mb-4">
        From the agent list, copy the{' '}
        <span className="mono text-blue-600 dark:text-blue-400">MCP config</span> URL and
        paste it into your assistant.
      </p>

      <div className="mono flex items-center justify-between gap-3 px-3.5 py-3 mb-4 rounded-xl border border-gray-700/30 dark:border-gray-700 bg-gray-50 dark:bg-gray-900">
        <code
          data-testid="onboarding-mcp-url"
          className="text-[12px] text-cyan-600 dark:text-cyan-400 truncate min-w-0"
        >
          {url}
        </code>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? 'MCP URL copied to clipboard' : 'Copy MCP URL to clipboard'}
          data-testid="onboarding-copy-button"
          className="text-[11px] text-gray-500 dark:text-gray-400 border border-gray-300 dark:border-gray-700 px-2.5 py-1 rounded-md flex-shrink-0 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>

      <div className="mono text-[11px] uppercase tracking-[0.1em] text-gray-400 dark:text-gray-500 mb-2.5">
        Supported clients
      </div>
      <div className="grid grid-cols-5 gap-2">
        {CLIENTS.map((client) => (
          <div
            key={client}
            className="px-2 py-2.5 border border-gray-200 dark:border-gray-700 rounded-lg text-center text-[11px] text-gray-500 dark:text-gray-400"
          >
            {client}
          </div>
        ))}
      </div>
    </div>
  );
}
