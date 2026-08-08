import type { CursorCollectionError } from '@/hooks/useOpaqueCursorCollection';
import {
  PolicyGovernanceApiError,
} from '@/services/policy-governance-api';

export function formatPolicyToken(value: string): string {
  return value
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

export function formatPolicyTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function createPolicyUiId(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `${prefix}-${uuid}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function policyUiErrorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    return error.nextAction
      ? `${error.message} Next: ${error.nextAction}.`
      : error.message;
  }
  return error instanceof Error
    ? error.message
    : 'Unexpected policy error.';
}

export function classifyPolicyCursorError(
  error: unknown,
): CursorCollectionError {
  if (
    error instanceof PolicyGovernanceApiError
    && error.kind === 'invalid_cursor'
  ) {
    return {
      message: 'This cursor expired or no longer matches the active filters.',
      restartRequired: true,
    };
  }
  return {
    message: policyUiErrorMessage(error),
    restartRequired: false,
  };
}
