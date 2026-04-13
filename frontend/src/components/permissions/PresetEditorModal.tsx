/**
 * PresetEditorModal — Create/edit/view a permission preset with granular flag toggles.
 */

import { useState } from 'react';
import { X, Shield } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { PermissionFlagsEditor, countAllFlags, setAllFlags } from './PermissionFlagsEditor';
import type { FlagsMap } from './PermissionFlagsEditor';
import type { PermissionPreset } from '@/types';

interface PresetEditorModalProps {
  /** Preset to edit. Null = create new. */
  preset: PermissionPreset | null;
  /** Base preset flags for "Reset to Base" (when cloned). */
  baseFlags?: FlagsMap | null;
  /** Template flags for new preset (all false by default). */
  templateFlags?: FlagsMap;
  onClose: () => void;
  onSaved: () => void;
}

export function PresetEditorModal({ preset, baseFlags, templateFlags, onClose, onSaved }: PresetEditorModalProps) {
  const api = useDashboardApi();
  const isBuiltIn = preset?.is_builtin ?? false;
  const isNew = !preset;

  const [name, setName] = useState(preset?.name || '');
  const [description, setDescription] = useState(preset?.description || '');
  const [flags, setFlags] = useState<FlagsMap>(
    preset?.flags || templateFlags || {}
  );
  const [saving, setSaving] = useState(false);

  const { total, enabled } = countAllFlags(flags);

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error('Preset name is required');
      return;
    }
    setSaving(true);
    try {
      if (isNew) {
        await api.createPreset({ name: name.trim(), description: description.trim() || undefined, flags });
        toast.success('Preset created');
      } else {
        await api.updatePreset(preset!.id, { name: name.trim(), description: description.trim() || undefined, flags });
        toast.success('Preset updated');
      }
      onSaved();
      onClose();
    } catch (err: any) {
      toast.error(err?.message || 'Failed to save preset');
    } finally {
      setSaving(false);
    }
  };

  const handleCloneBuiltIn = async () => {
    if (!preset) return;
    setSaving(true);
    try {
      await api.createPreset({
        name: `${preset.name} (copy)`,
        description: `Cloned from ${preset.name}`,
        flags: JSON.parse(JSON.stringify(preset.flags)),
      });
      toast.success('Preset cloned — you can now edit it');
      onSaved();
      onClose();
    } catch (err: any) {
      toast.error(err?.message || 'Failed to clone');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60]" onClick={onClose}>
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl flex flex-col max-h-[85vh]" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <div>
            <div className="flex items-center gap-2">
              <Shield size={18} className="text-violet-500" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                {isNew ? 'New Preset' : isBuiltIn ? `View: ${preset.name}` : `Edit: ${preset.name}`}
              </h2>
              {isBuiltIn && (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400 font-medium">
                  view only
                </span>
              )}
              {!isBuiltIn && !isNew && (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-violet-100 text-violet-600 font-medium">custom</span>
              )}
            </div>
            <p className="text-xs text-gray-400 mt-0.5">{enabled} of {total} flags enabled</p>
          </div>
          <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X size={20} />
          </button>
        </div>

        {/* Name / Description */}
        {!isBuiltIn && (
          <div className="px-6 py-3 border-b border-gray-100 dark:border-gray-700 space-y-2 shrink-0">
            <div className="flex gap-3">
              <div className="flex-1">
                <label className="text-[10px] text-gray-400 uppercase tracking-wide">Name</label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Preset name..."
                  className="w-full px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-lg text-sm dark:bg-gray-700 dark:text-gray-200 mt-0.5"
                />
              </div>
              <div className="flex-1">
                <label className="text-[10px] text-gray-400 uppercase tracking-wide">Description</label>
                <input
                  type="text"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="What this preset is for..."
                  className="w-full px-3 py-1.5 border border-gray-300 dark:border-gray-600 rounded-lg text-sm dark:bg-gray-700 dark:text-gray-200 mt-0.5"
                />
              </div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => setFlags(setAllFlags(flags, true))} className="text-[10px] px-2 py-1 rounded bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-300">
                Enable All
              </button>
              <button onClick={() => setFlags(setAllFlags(flags, false))} className="text-[10px] px-2 py-1 rounded bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-300">
                Disable All
              </button>
              {baseFlags && (
                <button onClick={() => setFlags(JSON.parse(JSON.stringify(baseFlags)))} className="text-[10px] px-2 py-1 rounded bg-blue-100 text-blue-700 hover:bg-blue-200 dark:bg-blue-900/30 dark:text-blue-300">
                  Reset to Base
                </button>
              )}
            </div>
          </div>
        )}

        {/* Flags editor */}
        <div className="flex-1 overflow-y-auto">
          <PermissionFlagsEditor flags={flags} onChange={isBuiltIn ? undefined : setFlags} readOnly={isBuiltIn} />
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700 shrink-0">
          <span className="text-xs text-gray-400">{enabled} of {total} flags enabled</span>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
              {isBuiltIn ? 'Close' : 'Cancel'}
            </button>
            {isBuiltIn ? (
              <button onClick={handleCloneBuiltIn} disabled={saving} className="px-4 py-2 text-sm bg-violet-500 text-white rounded-lg hover:bg-violet-600 font-medium disabled:opacity-50">
                {saving ? 'Cloning...' : 'Clone to customize'}
              </button>
            ) : (
              <button onClick={handleSave} disabled={saving || !name.trim()} className="px-4 py-2 text-sm bg-violet-500 text-white rounded-lg hover:bg-violet-600 font-medium disabled:opacity-50">
                {saving ? 'Saving...' : isNew ? 'Create Preset' : 'Save Preset'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
