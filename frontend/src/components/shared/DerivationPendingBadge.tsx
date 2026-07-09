import { GitBranch } from 'lucide-react';

import type {
  IdeationComplexity,
  IdeationStatus,
  RefinementStatus,
} from '@/types';

export const IDEATION_PENDING_REFINEMENT_LABEL = 'No refinement';
export const REFINEMENT_PENDING_SPEC_LABEL = 'No spec';

interface DerivationChild {
  status?: string | null;
  archived?: boolean | null;
  refinement_id?: string | null;
}

interface IdeationDerivationSource {
  status: IdeationStatus;
  complexity?: IdeationComplexity | null;
  active_refinement_count?: number | null;
  active_spec_count?: number | null;
  refinements?: DerivationChild[] | null;
  specs?: DerivationChild[] | null;
}

interface RefinementDerivationSource {
  status: RefinementStatus;
  active_spec_count?: number | null;
  specs?: DerivationChild[] | null;
}

interface DerivationPendingBadgeProps {
  label: string | null | undefined;
  compact?: boolean;
  className?: string;
}

export function isActiveDerivationChild(child: DerivationChild): boolean {
  return !child.archived && child.status !== 'cancelled';
}

export function countActiveDerivations(children: DerivationChild[] | null | undefined): number {
  return (children || []).filter(isActiveDerivationChild).length;
}

function resolveActiveCount(
  summaryCount: number | null | undefined,
  children: DerivationChild[] | null | undefined,
): number {
  return children ? countActiveDerivations(children) : summaryCount ?? 0;
}

function countActiveDirectSpecs(children: DerivationChild[] | null | undefined): number {
  return (children || []).filter(
    (child) => isActiveDerivationChild(child) && child.refinement_id == null,
  ).length;
}

function resolveActiveDirectSpecCount(
  summaryCount: number | null | undefined,
  children: DerivationChild[] | null | undefined,
): number {
  return children ? countActiveDirectSpecs(children) : summaryCount ?? 0;
}

export function getIdeationPendingDerivationLabel(
  ideation: IdeationDerivationSource,
): string | null {
  if (ideation.status !== 'done') {
    return null;
  }
  if (ideation.complexity === 'small') {
    const activeSpecs = resolveActiveDirectSpecCount(ideation.active_spec_count, ideation.specs);
    return activeSpecs === 0 ? REFINEMENT_PENDING_SPEC_LABEL : null;
  }
  if (ideation.complexity !== 'medium' && ideation.complexity !== 'large') {
    return null;
  }
  const activeRefinements = resolveActiveCount(
    ideation.active_refinement_count,
    ideation.refinements,
  );
  return activeRefinements === 0 ? IDEATION_PENDING_REFINEMENT_LABEL : null;
}

export function getRefinementPendingDerivationLabel(
  refinement: RefinementDerivationSource,
): string | null {
  if (refinement.status !== 'done') {
    return null;
  }
  const activeSpecs = resolveActiveCount(refinement.active_spec_count, refinement.specs);
  return activeSpecs === 0 ? REFINEMENT_PENDING_SPEC_LABEL : null;
}

export function DerivationPendingBadge({
  label,
  compact = false,
  className = '',
}: DerivationPendingBadgeProps) {
  if (!label) {
    return null;
  }

  const sizeClasses = compact
    ? 'gap-0.5 px-1.5 py-0.5 text-[10px]'
    : 'gap-1 px-2 py-0.5 text-[10px]';

  return (
    <span
      data-testid="derivation-pending-badge"
      role="status"
      title={label}
      aria-label={label}
      className={`inline-flex items-center shrink-0 rounded-full font-semibold uppercase tracking-wide bg-cyan-100 text-cyan-800 dark:bg-cyan-900/40 dark:text-cyan-200 ${sizeClasses} ${className}`}
    >
      <GitBranch className="h-3 w-3 shrink-0" aria-hidden />
      <span>{label}</span>
    </span>
  );
}
