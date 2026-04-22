/**
 * MentionInput - Text input with @mention autocomplete dropdown
 */

import { useEffect, useRef, useState } from 'react';

export interface Mentionable {
  id: string;
  name: string;
  type: 'agent' | 'user';
}

interface MentionInputProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: () => void;
  placeholder?: string;
  mentionables: Mentionable[];
  className?: string;
  multiline?: boolean;
  rows?: number;
  autoFocus?: boolean;
}

const INPUT_CLASSES =
  'w-full px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white dark:bg-gray-700 dark:border-gray-600 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent';

export function MentionInput({
  value,
  onChange,
  onSubmit,
  placeholder,
  mentionables,
  className = '',
  multiline = false,
  rows = 1,
  autoFocus = false,
}: MentionInputProps) {
  const [showDropdown, setShowDropdown] = useState(false);
  const [filter, setFilter] = useState('');
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [cursorPos, setCursorPos] = useState(0);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  const filtered = mentionables.filter((m) =>
    m.name.toLowerCase().includes(filter.toLowerCase())
  );

  useEffect(() => {
    setSelectedIdx(0);
  }, [filter]);

  const getMentionContext = (text: string, pos: number): { start: number; query: string } | null => {
    let i = pos - 1;
    while (i >= 0) {
      if (text[i] === '@') {
        const query = text.slice(i + 1, pos);
        if (query.includes(' ') && query.length > 20) return null;
        return { start: i, query };
      }
      if (text[i] === ' ' && i < pos - 1) {
        if (pos - i > 30) return null;
      }
      if (text[i] === '\n') return null;
      i--;
    }
    return null;
  };

  const handleChange = (newValue: string, newCursorPos: number) => {
    onChange(newValue);
    setCursorPos(newCursorPos);

    const ctx = getMentionContext(newValue, newCursorPos);
    if (ctx) {
      setFilter(ctx.query);
      setShowDropdown(true);
    } else {
      setShowDropdown(false);
    }
  };

  const insertMention = (mentionable: Mentionable) => {
    const ctx = getMentionContext(value, cursorPos);
    if (!ctx) return;

    const before = value.slice(0, ctx.start);
    const after = value.slice(cursorPos);
    const mention = `@${mentionable.name}`;
    const newValue = `${before}${mention} ${after}`;
    onChange(newValue);
    setShowDropdown(false);

    setTimeout(() => {
      const el = inputRef.current;
      if (el) {
        el.focus();
        const newPos = before.length + mention.length + 1;
        el.setSelectionRange(newPos, newPos);
      }
    }, 0);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (showDropdown && filtered.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIdx((prev) => (prev + 1) % filtered.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIdx((prev) => (prev - 1 + filtered.length) % filtered.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        insertMention(filtered[selectedIdx]);
        return;
      }
      if (e.key === 'Escape') {
        setShowDropdown(false);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !showDropdown && onSubmit) {
      e.preventDefault();
      onSubmit();
    }
  };

  const sharedProps = {
    ref: inputRef as any,
    value,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      handleChange(e.target.value, e.target.selectionStart || 0);
    },
    onKeyDown: handleKeyDown,
    onSelect: (e: React.SyntheticEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setCursorPos((e.target as HTMLInputElement).selectionStart || 0);
    },
    onClick: () => {
      const el = inputRef.current;
      if (el) setCursorPos(el.selectionStart || 0);
    },
    placeholder,
    className: INPUT_CLASSES,
    autoFocus,
  };

  return (
    <div className={`relative ${className}`}>
      {multiline ? (
        <textarea {...sharedProps} rows={rows} />
      ) : (
        <input type="text" {...sharedProps} />
      )}

      {showDropdown && filtered.length > 0 && (
        <div className="absolute left-0 top-full mt-1 w-64 max-h-48 overflow-y-auto bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-[60]">
          {filtered.map((m, idx) => (
            <button
              key={m.id}
              onMouseDown={(e) => {
                e.preventDefault();
                insertMention(m);
              }}
              className={`flex items-center gap-2 w-full px-3 py-2 text-left text-sm transition-colors ${
                idx === selectedIdx
                  ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
                  : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
              }`}
            >
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 ${
                m.type === 'agent'
                  ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300'
                  : 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
              }`}>
                {m.type === 'agent' ? 'A' : 'U'}
              </span>
              <span className="truncate">{m.name}</span>
              <span className="text-[10px] text-gray-400 ml-auto shrink-0">{m.type}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
