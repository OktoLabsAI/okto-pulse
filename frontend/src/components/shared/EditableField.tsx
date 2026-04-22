/**
 * EditableField — Inline editing component with view/edit mode toggle.
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import { Pencil } from 'lucide-react';

interface EditableFieldProps {
  value: string;
  onSave: (value: string) => Promise<void> | void;
  multiline?: boolean;
  placeholder?: string;
  renderView?: (value: string) => React.ReactNode;
}

export function EditableField({
  value,
  onSave,
  multiline = false,
  placeholder = 'Click to edit...',
  renderView,
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      // Place cursor at end
      const len = inputRef.current.value.length;
      inputRef.current.setSelectionRange(len, len);
    }
  }, [editing]);

  const handleSave = useCallback(async () => {
    const trimmed = draft.trim();
    if (trimmed === value) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(trimmed);
      setEditing(false);
    } catch {
      // Keep editing on error
    } finally {
      setSaving(false);
    }
  }, [draft, value, onSave]);

  const handleCancel = useCallback(() => {
    setDraft(value);
    setEditing(false);
  }, [value]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        handleCancel();
        return;
      }
      if (multiline) {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          handleSave();
        }
      } else {
        if (e.key === 'Enter') {
          e.preventDefault();
          handleSave();
        }
      }
    },
    [multiline, handleSave, handleCancel]
  );

  if (editing) {
    return (
      <div className="relative">
        {multiline ? (
          <textarea
            ref={inputRef as React.RefObject<HTMLTextAreaElement>}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={handleSave}
            onKeyDown={handleKeyDown}
            disabled={saving}
            rows={6}
            className="w-full px-3 py-2 border border-blue-300 dark:border-blue-600 rounded-lg text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-400 resize-y disabled:opacity-50"
            placeholder={placeholder}
          />
        ) : (
          <input
            ref={inputRef as React.RefObject<HTMLInputElement>}
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={handleSave}
            onKeyDown={handleKeyDown}
            disabled={saving}
            className="w-full px-3 py-1.5 border border-blue-300 dark:border-blue-600 rounded-lg text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-400 disabled:opacity-50"
            placeholder={placeholder}
          />
        )}
        <p className="text-[10px] text-gray-400 mt-1">
          {multiline ? 'Ctrl+Enter to save, Esc to cancel' : 'Enter to save, Esc to cancel'}
        </p>
      </div>
    );
  }

  return (
    <div
      onClick={() => setEditing(true)}
      className="group relative cursor-pointer rounded-lg border border-transparent hover:border-gray-200 dark:hover:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors px-1 -mx-1"
    >
      {value ? (
        renderView ? renderView(value) : <p className="text-sm text-gray-700 dark:text-gray-300">{value}</p>
      ) : (
        <p className="text-sm text-gray-400 dark:text-gray-500 italic">{placeholder}</p>
      )}
      <Pencil
        size={12}
        className="absolute top-1 right-1 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity"
      />
    </div>
  );
}
