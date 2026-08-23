/**
 * PresetListModal — List, create, clone, edit, delete permission presets.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { X, Plus, Shield, Copy, Pencil, Trash2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import {
  type ImportExportEnvelope,
  useImportExportApi,
} from '@/services/import-export-api';
import {
  ExportItemButton,
  ImportExportButtons,
} from '@/components/shared/ImportExportButtons';
import { countPerEntity, countAllFlags } from './PermissionFlagsEditor';
import { getEntityChipClasses, getEntityLabel } from './permissionLabels';
import { PresetEditorModal } from './PresetEditorModal';
import type { FlagsMap } from './PermissionFlagsEditor';
import { PresetLineageInfo } from './PresetLineageInfo';
import { disabledFullControlTemplate } from './presetResolution';
import type { PermissionPreset } from '@/types';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import { useCurrentBoard } from '@/store/dashboard';

interface PresetListModalProps {
  onClose: () => void;
  /** Optional explicit scope; existing callers fall back to the current board. */
  boardId?: string | null;
}

export function PresetListModal({ onClose, boardId }: PresetListModalProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const importExportApi = useImportExportApi();
  const importExportRef = useRef(importExportApi);
  importExportRef.current = importExportApi;
  const currentBoard = useCurrentBoard();
  const resolvedBoardId = boardId ?? currentBoard?.id;
  const permissions = usePermissions(resolvedBoardId);
  const policyAuthorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canReadPresets = policyAuthorityReady
    && permissions.has('permission_preset.entity.read');
  const canCreatePreset = policyAuthorityReady
    && permissions.has('permission_preset.entity.create');
  const canEditPreset = policyAuthorityReady
    && permissions.has('permission_preset.entity.edit');
  const canDeletePreset = policyAuthorityReady
    && permissions.has('permission_preset.entity.delete');
  const canClonePreset = policyAuthorityReady
    && permissions.has('permission_preset.clone');
  const canImportPresets = policyAuthorityReady
    && permissions.has('permission_preset.import');
  const canExportPresets = policyAuthorityReady
    && permissions.has('permission_preset.export');
  const [presets, setPresets] = useState<PermissionPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [editorPreset, setEditorPreset] = useState<PermissionPreset | null | 'new'>(null);

  useEscapeToClose(onClose);

  const loadPresets = useCallback(async () => {
    if (!policyAuthorityReady) {
      setPresets([]);
      setLoading(permissions.isLoading);
      return;
    }
    if (!canReadPresets) {
      setPresets([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const data = await apiRef.current.listPresets();
      setPresets(data);
    } catch { toast.error('Failed to load presets'); }
    finally { setLoading(false); }
  }, [canReadPresets, permissions.isLoading, policyAuthorityReady]);

  useEffect(() => { void loadPresets(); }, [loadPresets]);

  const handleClone = async (preset: PermissionPreset) => {
    if (!canClonePreset) return;
    try {
      await apiRef.current.clonePreset(preset.id, {
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
    if (!canDeletePreset) return;
    if (!confirm(`Delete preset "${preset.name}"? This cannot be undone.`)) return;
    try {
      await apiRef.current.deletePreset(preset.id);
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
              <ImportExportButtons
                kind="presets"
                onExport={() => importExportRef.current.exportPresets()}
                onImport={(envelope, options) => importExportRef.current.importPresets(envelope, options)}
                onImported={() => loadPresets()}
                confirmReplacements
                canExport={canExportPresets}
                canImport={canImportPresets}
              />
              <button
                disabled={!canCreatePreset}
                onClick={() => {
                  if (canCreatePreset) setEditorPreset('new');
                }}
                className="px-3 py-1.5 bg-violet-500 text-white rounded-lg text-sm font-medium hover:bg-violet-600 disabled:cursor-not-allowed disabled:opacity-50 flex items-center gap-1"
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
            ) : !canReadPresets ? (
              <div data-testid="preset-read-unavailable" className="text-center py-8 text-gray-500 dark:text-gray-400">
                Presets are hidden until <code>permission_preset.entity.read</code> is granted.
              </div>
            ) : (
              <>
                {/* Built-in */}
                <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Built-in Presets</h3>
                {builtIn.map((preset) => (
                  <PresetCard
                    key={preset.id}
                    preset={preset}
                    presets={presets}
                    onView={() => setEditorPreset(preset)}
                    onClone={() => handleClone(preset)}
                    onExport={() => importExportRef.current.exportPreset(preset.id)}
                    canClone={canClonePreset}
                    canExport={canExportPresets}
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
                      presets={presets}
                      onView={() => setEditorPreset(preset)}
                      onClone={() => handleClone(preset)}
                      onExport={() => importExportRef.current.exportPreset(preset.id)}
                      onEdit={() => setEditorPreset(preset)}
                      onDelete={() => handleDelete(preset)}
                      canClone={canClonePreset}
                      canEdit={canEditPreset}
                      canDelete={canDeletePreset}
                      canExport={canExportPresets}
                    />
                  ))
                )}

                <button
                  disabled={!canCreatePreset}
                  onClick={() => {
                    if (canCreatePreset) setEditorPreset('new');
                  }}
                  className="w-full py-3 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-xl text-gray-400 hover:border-violet-400 hover:text-violet-500 disabled:cursor-not-allowed disabled:opacity-50 flex items-center justify-center gap-2 text-sm"
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
          boardId={resolvedBoardId}
          presets={presets}
          templateFlags={
            editorPreset === 'new'
              ? disabledFullControlTemplate(presets) ?? undefined
              : undefined
          }
          onClose={() => setEditorPreset(null)}
          onSaved={loadPresets}
        />
      )}
    </>
  );
}

function PresetCard({
  preset,
  presets,
  onView,
  onClone,
  onEdit,
  onDelete,
  onExport,
  canClone,
  canEdit = false,
  canDelete = false,
  canExport,
}: {
  preset: PermissionPreset;
  presets: readonly PermissionPreset[];
  onView: () => void;
  onClone: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
  onExport: () => Promise<ImportExportEnvelope>;
  canClone: boolean;
  canEdit?: boolean;
  canDelete?: boolean;
  canExport: boolean;
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
            {preset.owner_review_required && (
              <span
                className="text-[10px] px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                title={preset.review_reason || 'Preset lineage requires owner review'}
              >
                owner review
              </span>
            )}
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
          <ExportItemButton
            kind="presets"
            itemId={preset.id}
            itemLabel={preset.name}
            onExport={onExport}
            canExport={canExport}
          />
          {onEdit && (
            <button disabled={!canEdit} onClick={onEdit} className="p-1 text-gray-400 hover:text-blue-500 disabled:opacity-40" title="Edit">
              <Pencil size={13} />
            </button>
          )}
          <button disabled={!canClone} onClick={onClone} className="p-1 text-gray-400 hover:text-blue-500 disabled:opacity-40" title="Clone">
            <Copy size={13} />
          </button>
          {onDelete && (
            <button disabled={!canDelete} onClick={onDelete} className="p-1 text-gray-400 hover:text-red-500 disabled:opacity-40" title="Delete">
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>
      {/* Entity breakdown */}
      <div className="flex flex-wrap gap-1.5 mt-2.5">
        {Object.entries(perEntity).map(([entity, { total: t, enabled: e }]) => (
          <span key={entity} className={`text-[10px] px-2 py-0.5 rounded ${getEntityChipClasses(entity)}`}>
            {getEntityLabel(entity)} {e}/{t}
          </span>
        ))}
      </div>
      <PresetLineageInfo preset={preset} presets={presets} compact />
    </div>
  );
}
