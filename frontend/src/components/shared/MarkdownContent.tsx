/**
 * MarkdownContent - Renders markdown text with GitHub-flavored markdown and Mermaid diagram support.
 * Code blocks with language "mermaid" are rendered as SVG diagrams.
 */

import { useEffect, useRef, useState, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import mermaid from 'mermaid';

// Initialize mermaid once
mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
  fontFamily: 'inherit',
});

let mermaidCounter = 0;

/** Renders a single Mermaid diagram from source text */
const MermaidBlock = memo(function MermaidBlock({ chart }: { chart: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');
  const idRef = useRef(`mermaid-${++mermaidCounter}`);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { svg: rendered } = await mermaid.render(idRef.current, chart.trim());
        if (!cancelled) {
          setSvg(rendered);
          setError('');
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Failed to render diagram');
          setSvg('');
        }
        // Clean up mermaid's error container
        document.getElementById('d' + idRef.current)?.remove();
      }
    })();
    return () => { cancelled = true; };
  }, [chart]);

  if (error) {
    return (
      <div className="border border-red-200 dark:border-red-800 rounded-lg p-3 my-2 bg-red-50 dark:bg-red-900/20">
        <p className="text-xs text-red-600 dark:text-red-400 font-medium mb-1">Mermaid diagram error</p>
        <pre className="text-xs text-red-500 whitespace-pre-wrap">{error}</pre>
        <details className="mt-2">
          <summary className="text-xs text-gray-500 cursor-pointer">Source</summary>
          <pre className="text-xs text-gray-500 mt-1 whitespace-pre-wrap">{chart}</pre>
        </details>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 my-2 text-center text-gray-400 text-xs animate-pulse">
        Rendering diagram...
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="my-3 flex justify-center overflow-x-auto bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-3"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
});

interface MarkdownContentProps {
  content: string;
  className?: string;
}

export function MarkdownContent({ content, className = '' }: MarkdownContentProps) {
  return (
    <div className={`prose prose-sm dark:prose-invert max-w-none
      prose-headings:text-gray-800 dark:prose-headings:text-gray-200
      prose-headings:font-semibold prose-headings:mt-3 prose-headings:mb-1
      prose-h2:text-base prose-h3:text-sm
      prose-p:text-gray-600 dark:prose-p:text-gray-400 prose-p:my-1
      prose-li:text-gray-600 dark:prose-li:text-gray-400 prose-li:my-0.5
      prose-strong:text-gray-700 dark:prose-strong:text-gray-300
      prose-code:text-xs prose-code:bg-gray-100 dark:prose-code:bg-gray-800
      prose-code:px-1 prose-code:py-0.5 prose-code:rounded
      prose-pre:bg-gray-50 dark:prose-pre:bg-gray-800 prose-pre:text-xs
      prose-a:text-blue-600 dark:prose-a:text-blue-400
      prose-ul:my-1 prose-ol:my-1
      ${className}`}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className: codeClassName, children, ...props }) {
            const match = /language-mermaid/.exec(codeClassName || '');
            if (match) {
              return <MermaidBlock chart={String(children).replace(/\n$/, '')} />;
            }
            return <code className={codeClassName} {...props}>{children}</code>;
          },
          pre({ children }) {
            return <>{children}</>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
