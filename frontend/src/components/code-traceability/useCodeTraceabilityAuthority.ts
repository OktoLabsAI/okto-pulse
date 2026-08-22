import { usePermissions } from '@/hooks/usePermissions';

export const CODE_TRACEABILITY_PROJECTION_READ_LEAVES = [
  'code_traceability.investigation.read',
  'code_traceability.evidence.read',
  'code_traceability.target.read',
  'code_traceability.overlap.read',
] as const;

export const CODE_TRACEABILITY_RECEIPT_REVOKE_LEAF =
  'code_traceability.investigation.revoke' as const;
export const CODE_TRACEABILITY_EVIDENCE_REVOKE_LEAF =
  'code_traceability.evidence.revoke' as const;
export const CODE_TRACEABILITY_EVIDENCE_CLASSIFY_LEGACY_LEAF =
  'code_traceability.evidence.classify_legacy' as const;
export const CODE_TRACEABILITY_TARGET_CREATE_LEAF =
  'code_traceability.target.create' as const;
export const CODE_TRACEABILITY_OVERLAP_ACKNOWLEDGE_LEAF =
  'code_traceability.overlap.acknowledge' as const;
export const CODE_TRACEABILITY_WAIVER_CREATE_LEAF =
  'code_traceability.waiver.create' as const;
export const CODE_TRACEABILITY_WAIVER_CLEAR_LEAF =
  'code_traceability.waiver.clear' as const;

/**
 * Single fail-closed authority projection shared by every Code Traceability
 * tab and each separately governed human mutation surface.
 */
export function useCodeTraceabilityAuthority(
  boardId: string | null | undefined,
) {
  const permissions = usePermissions(boardId);
  const authorityReady = Boolean(boardId)
    && !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired;
  return {
    canReadProjection: authorityReady && CODE_TRACEABILITY_PROJECTION_READ_LEAVES.every(
      permissions.has,
    ),
    canRevokeReceipt: authorityReady && permissions.has(
      CODE_TRACEABILITY_RECEIPT_REVOKE_LEAF,
    ),
    canRevokeEvidence: authorityReady && permissions.has(
      CODE_TRACEABILITY_EVIDENCE_REVOKE_LEAF,
    ),
    canClassifyLegacyEvidence: authorityReady && permissions.has(
      CODE_TRACEABILITY_EVIDENCE_CLASSIFY_LEGACY_LEAF,
    ),
    canCreateTarget: authorityReady && permissions.has(
      CODE_TRACEABILITY_TARGET_CREATE_LEAF,
    ),
    canAcknowledgeOverlap: authorityReady && permissions.has(
      CODE_TRACEABILITY_OVERLAP_ACKNOWLEDGE_LEAF,
    ),
    canCreateWaiver: authorityReady && permissions.has(
      CODE_TRACEABILITY_WAIVER_CREATE_LEAF,
    ),
    canClearWaiver: authorityReady && permissions.has(
      CODE_TRACEABILITY_WAIVER_CLEAR_LEAF,
    ),
    isLoading: permissions.isLoading,
    error: permissions.error,
  };
}
