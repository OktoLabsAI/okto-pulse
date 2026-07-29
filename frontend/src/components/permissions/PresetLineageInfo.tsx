import { AlertTriangle, GitBranch } from 'lucide-react';
import type { PermissionPreset } from '@/types';
import { resolvePresetLineage } from './presetResolution';

interface PresetLineageInfoProps {
  preset: PermissionPreset;
  presets: readonly PermissionPreset[];
  compact?: boolean;
}

export function PresetLineageInfo({
  preset,
  presets,
  compact = false,
}: PresetLineageInfoProps) {
  if (preset.is_builtin) return null;
  const lineage = resolvePresetLineage(preset, presets);
  const requiresReview = lineage.ownerReviewRequired;

  return (
    <div
      data-testid={`preset-lineage-${preset.id}`}
      className={`rounded-lg border ${
        compact ? 'mt-2 px-2.5 py-1.5' : 'px-3 py-2'
      } ${
        requiresReview
          ? 'border-red-200 bg-red-50/80 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300'
          : 'border-blue-200 bg-blue-50/70 text-blue-700 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-300'
      }`}
    >
      <div className="flex items-center gap-1.5 text-[11px] font-medium">
        {requiresReview ? (
          <AlertTriangle size={12} aria-hidden="true" />
        ) : (
          <GitBranch size={12} aria-hidden="true" />
        )}
        <span
          title={
            preset.base_preset_id
              ? `Base preset ID: ${preset.base_preset_id}`
              : 'No base_preset_id'
          }
        >
          Base: <strong>{lineage.baseLabel}</strong>
        </span>
        <span aria-hidden="true">·</span>
        <span>{lineage.stateLabel}</span>
        {requiresReview && (
          <>
            <span aria-hidden="true">·</span>
            <span>owner review required</span>
          </>
        )}
      </div>
      {!compact && (
        <p className="mt-1 text-[10px] opacity-80">
          Lineage: {lineage.chainLabel}
          {preset.base_preset_id ? ` · Base ID: ${preset.base_preset_id}` : ''}
          {lineage.reviewReason ? ` · ${lineage.reviewReason}` : ''}
        </p>
      )}
    </div>
  );
}
