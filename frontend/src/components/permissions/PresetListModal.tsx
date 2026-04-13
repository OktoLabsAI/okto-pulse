/**
 * PresetListModal — List, create, clone, edit, delete permission presets.
 */

import { useEffect, useState } from 'react';
import { X, Plus, Shield, Copy, Pencil, Trash2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { countPerEntity, ENTITY_LABELS, countAllFlags } from './PermissionFlagsEditor';
import { PresetEditorModal } from './PresetEditorModal';
import type { FlagsMap } from './PermissionFlagsEditor';
import type { PermissionPreset } from '@/types';

interface PresetListModalProps {
  onClose: () => void;
}

const ENTITY_BG: Record<string, string> = {
  board: 'bg-blue-50 text-blue-600 dark:bg-blue-900/20 dark:text-blue-300',
  spec: 'bg-violet-50 text-violet-600 dark:bg-violet-900/20 dark:text-violet-300',
  card: 'bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-300',
  ideation: 'bg-amber-50 text-amber-600 dark:bg-amber-900/20 dark:text-amber-300',
  refinement: 'bg-cyan-50 text-cyan-600 dark:bg-cyan-900/20 dark:text-cyan-300',
  profile: 'bg-gray-50 text-gray-600 dark:bg-gray-700/50 dark:text-gray-400',
  guidelines: 'bg-pink-50 text-pink-600 dark:bg-pink-900/20 dark:text-pink-300',
};

export function PresetListModal({ onClose }: PresetListModalProps) {
  const api = useDashboardApi();
  const [presets, setPresets] = useState<PermissionPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [editorPreset, setEditorPreset] = useState<PermissionPreset | null | 'new'>(null);

  useEffect(() => { loadPresets(); }, []);

  const loadPresets = async () => {
    setLoading(true);
    try {
      const data = await api.listPresets();
      setPresets(data);
    } catch { toast.error('Failed to load presets'); }
    finally { setLoading(false); }
  };

  const handleClone = async (preset: PermissionPreset) => {
    try {
      await api.createPreset({
        name: `${preset.name} (copy)`,
        description: `Cloned from ${preset.name}`,
        flags: JSON.parse(JSON.stringify(preset.flags)),
      });
      toast.success('Preset cloned');
      await loadPresets();
    } catch (err: any) {
      toast.error(err?.message || 'Failed to clone');
    }
  };

  const handleDelete = async (preset: PermissionPreset) => {
    if (!confirm(`Delete preset "${preset.name}"? This cannot be undone.`)) return;
    try {
      await api.deletePreset(preset.id);
      toast.success('Preset deleted');
      await loadPresets();
    } catch (err: any) {
      toast.error(err?.message || 'Failed to delete');
    }
  };

  const builtIn = presets.filter((p) => p.is_builtin);
  const custom = presets.filter((p) => !p.is_builtin);

  return (
    <>
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-3xl flex flex-col max-h-[80vh]" onClick={(e) => e.stopPropagation()}>
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
            <div className="flex items-center gap-2">
              <Shield size={20} className="text-violet-500" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Permission Presets</h2>
              <span className="text-xs text-gray-400">({presets.length})</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setEditorPreset('new')}
                className="px-3 py-1.5 bg-violet-500 text-white rounded-lg text-sm font-medium hover:bg-violet-600 flex items-center gap-1"
              >
                <Plus size={14} />
                New Preset
              </button>
              <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                <X size={20} />
              </button>
            </div>
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto p-6 space-y-3">
            {loading ? (
              <div className="text-center py-8 text-gray-500 dark:text-gray-400">Loading presets...</div>
            ) : (
              <>
                {/* Built-in */}
                <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Built-in Presets</h3>
                {builtIn.map((preset) => (
                  <PresetCard
                    key={preset.id}
                    preset={preset}
                    onView={() => setEditorPreset(preset)}
                    onClone={() => handleClone(preset)}
                  />
                ))}

                {/* Custom */}
                <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mt-6">Custom Presets</h3>
                {custom.length === 0 ? (
                  <p className="text-xs text-gray-400 italic">No custom presets yet. Clone a built-in or create from scratch.</p>
                ) : (
                  custom.map((preset) => (
                    <PresetCard
                      key={preset.id}
                      preset={preset}
                      onView={() => setEditorPreset(preset)}
                      onClone={() => handleClone(preset)}
                      onEdit={() => setEditorPreset(preset)}
                      onDelete={() => handleDelete(preset)}
                    />
                  ))
                )}

                <button
                  onClick={() => setEditorPreset('new')}
                  className="w-full py-3 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-xl text-gray-400 hover:border-violet-400 hover:text-violet-500 flex items-center justify-center gap-2 text-sm"
                >
                  <Plus size={14} />
                  Create custom preset
                </button>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Editor overlay */}
      {editorPreset !== null && (
        <PresetEditorModal
          preset={editorPreset === 'new' ? null : editorPreset}
          baseFlags={editorPreset !== 'new' && !editorPreset.is_builtin ? (builtIn[0]?.flags as FlagsMap) : null}
          templateFlags={editorPreset === 'new' && builtIn[0] ? (() => {
            // New preset: start with all flags from Full Control but set to false
            const template = JSON.parse(JSON.stringify(builtIn[0].flags));
            const setFalse = (obj: Record<string, any>) => {
              for (const key of Object.keys(obj)) {
                if (typeof obj[key] === 'boolean') obj[key] = false;
                else if (typeof obj[key] === 'object') setFalse(obj[key]);
              }
            };
            setFalse(template);
            return template as FlagsMap;
          })() : undefined}
          onClose={() => setEditorPreset(null)}
          onSaved={loadPresets}
        />
      )}
    </>
  );
}

function PresetCard({
  preset,
  onView,
  onClone,
  onEdit,
  onDelete,
}: {
  preset: PermissionPreset;
  onView: () => void;
  onClone: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const flags = preset.flags as FlagsMap;
  const perEntity = countPerEntity(flags);
  const { total, enabled } = countAllFlags(flags);

  return (
    <div
      onClick={onView}
      className={`border rounded-xl p-4 cursor-pointer transition-colors ${
        preset.is_builtin
          ? 'border-gray-200 dark:border-gray-700 hover:border-violet-300 dark:hover:border-violet-600'
          : 'border-violet-200 dark:border-violet-700 bg-violet-50/30 dark:bg-violet-900/5 hover:border-violet-400'
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h4 className="font-medium text-gray-900 dark:text-white">{preset.name}</h4>
            <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
              preset.is_builtin
                ? 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                : 'bg-violet-100 text-violet-600 dark:bg-violet-900/40 dark:text-violet-300'
            }`}>
              {preset.is_builtin ? 'built-in' : 'custom'}
            </span>
          </div>
          {preset.description && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate">{preset.description}</p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-3" onClick={(e) => e.stopPropagation()}>
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
            enabled === total ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' :
            'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
          }`}>
            {enabled}/{total}
          </span>
          {onEdit && (
            <button onClick={onEdit} className="p-1 text-gray-400 hover:text-blue-500" title="Edit">
              <Pencil size={13} />
            </button>
          )}
          <button onClick={onClone} className="p-1 text-gray-400 hover:text-blue-500" title="Clone">
            <Copy size={13} />
          </button>
          {onDelete && (
            <button onClick={onDelete} className="p-1 text-gray-400 hover:text-red-500" title="Delete">
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>
      {/* Entity breakdown */}
      <div className="flex flex-wrap gap-1.5 mt-2.5">
        {Object.entries(perEntity).map(([entity, { total: t, enabled: e }]) => (
          <span key={entity} className={`text-[10px] px-2 py-0.5 rounded ${ENTITY_BG[entity] || 'bg-gray-50 text-gray-600'}`}>
            {ENTITY_LABELS[entity] || entity} {e}/{t}
          </span>
        ))}
      </div>
    </div>
  );
}
