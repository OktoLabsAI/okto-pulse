/**
 * SearchInput — controlled input with `/` focus shortcut, `Esc` clear,
 * and an inline X button. Pairs with `useListSearch` but is independent
 * (any controlled input pattern works).
 */

import { useCallback, useEffect, useRef } from 'react';
import { Search, X } from 'lucide-react';

interface SearchInputProps {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  /** Enable global `/` shortcut to focus this input. Default true. */
  enableSlashShortcut?: boolean;
  /** Test id for vitest selectors. */
  testId?: string;
  /** Optional className override for the wrapper. */
  className?: string;
}

export function SearchInput({
  value,
  onChange,
  placeholder = 'Search…',
  enableSlashShortcut = true,
  testId = 'list-search-input',
  className = '',
}: SearchInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        if (value) {
          e.preventDefault();
          onChange('');
          return;
        }
        inputRef.current?.blur();
      }
    },
    [value, onChange],
  );

  useEffect(() => {
    if (!enableSlashShortcut) return undefined;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== '/') return;
      const t = e.target as HTMLElement | null;
      if (!t) return;
      const tag = t.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (t as HTMLElement).isContentEditable) return;
      e.preventDefault();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [enableSlashShortcut]);

  return (
    <div className={`relative inline-flex items-center ${className}`}>
      <Search
        size={14}
        className="absolute left-2.5 text-gray-400 pointer-events-none"
        aria-hidden="true"
      />
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        data-testid={testId}
        aria-label={placeholder}
        className="pl-8 pr-7 py-1.5 text-sm rounded-lg border border-gray-200 dark:border-gray-700
          bg-white dark:bg-surface-800 text-gray-900 dark:text-gray-100
          focus:outline-none focus:ring-2 focus:ring-accent-500/40 focus:border-accent-500
          placeholder:text-gray-400 w-56"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange('')}
          className="absolute right-1.5 p-0.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
          aria-label="Clear search"
          data-testid={`${testId}-clear`}
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}
