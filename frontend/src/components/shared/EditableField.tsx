/**
 * EditableField — Inline editing component with view/edit mode toggle.
 */

import { useState, useRef, useEffect, useCallback } from 'react';
import { Pencil } from 'lucide-react';

interface EditableFieldProps {
  value: string;
  onSave: (value: string) => Promise<void> | void;
  disabled?: boolean;
  multiline?: boolean;
  placeholder?: string;
  renderView?: (value: string) => React.ReactNode;
}

export function EditableField({
  value,
  onSave,
  disabled = false,
  multiline = false,
  placeholder = 'Click to edit...',
  renderView,
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [base, setBase] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!editing) { setDraft(value); setBase(value); }
  }, [value, editing]);
  const conflict = editing && base !== value;

  useEffect(() => {
    if (disabled) setEditing(false);
  }, [disabled]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      // Place cursor at end
      const len = inputRef.current.value.length;
      inputRef.current.setSelectionRange(len, len);
    }
  }, [editing]);

  const handleSave = useCallback(async () => {
    // A live update never silently replaces or rebases an authored draft.
    if (disabled || conflict || saving) return;
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
  }, [draft, value, onSave, disabled, conflict, saving]);

  const handleCancel = useCallback(() => {
    setDraft(value);
    setBase(value);
    setEditing(false);
  }, [value]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
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
        {conflict && <div role="alert" className="mt-2 rounded border border-amber-300 p-2 text-xs text-amber-800 dark:border-amber-700 dark:text-amber-200">
          <p>The saved value changed while you were editing. Your draft has been preserved.</p>
          <p className="mt-1 whitespace-pre-wrap break-words">Current value: {value || '(empty)'}</p>
          <button type="button" className="btn btn-secondary mt-2" onClick={() => {
            setBase(value);
            inputRef.current?.focus();
          }}>Keep my draft after reviewing this value</button>
          <button type="button" className="btn btn-secondary mt-2 ml-2" onClick={handleCancel}>Use current value</button>
        </div>}
      </div>
    );
  }

  return (
    <div
      onClick={() => { if (!disabled) { setDraft(value); setBase(value); setEditing(true); } }}
      aria-disabled={disabled}
      className={`group relative rounded-lg border border-transparent transition-colors px-1 -mx-1 ${
        disabled
          ? 'cursor-default'
          : 'cursor-pointer hover:border-gray-200 dark:hover:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50'
      }`}
    >
      {value ? (
        renderView ? renderView(value) : <p className="text-sm text-gray-700 dark:text-gray-300">{value}</p>
      ) : (
        <p className="text-sm text-gray-400 dark:text-gray-500 italic">{placeholder}</p>
      )}
      {!disabled && (
        <Pencil
          size={12}
          className="absolute top-1 right-1 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity"
        />
      )}
    </div>
  );
}
