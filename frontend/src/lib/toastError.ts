import { createElement, isValidElement, type ReactElement } from 'react';
import toast from 'react-hot-toast';
import {
  PulseErrorMessage,
  type PulseErrorMessageProps,
} from '@/components/shared/PulseErrorMessage';

export { PulseErrorMessage } from '@/components/shared/PulseErrorMessage';

const SENSITIVE_KEY =
  /(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)/i;
const MAX_DEPTH = 4;
const MAX_ENTRIES = 30;
const MAX_ARRAY_ITEMS = 20;
const MAX_STRING_LENGTH = 600;
const MAX_SERIALIZED_LENGTH = 4000;

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null
    ? value as Record<string, unknown>
    : null;
}

function clippedString(value: string): string {
  return value.length <= MAX_STRING_LENGTH
    ? value
    : `${value.slice(0, MAX_STRING_LENGTH)}…`;
}

function sanitizedValue(
  value: unknown,
  depth: number,
  seen: WeakSet<object>,
): unknown {
  if (typeof value === 'string') return clippedString(value);
  if (
    value === null
    || typeof value === 'number'
    || typeof value === 'boolean'
  ) {
    return value;
  }
  if (typeof value === 'bigint') return String(value);
  if (typeof value !== 'object') return String(value);
  if (depth >= MAX_DEPTH) return '[depth limit]';
  if (seen.has(value)) return '[circular]';
  seen.add(value);

  if (Array.isArray(value)) {
    const result = value
      .slice(0, MAX_ARRAY_ITEMS)
      .map((item) => sanitizedValue(item, depth + 1, seen));
    if (value.length > MAX_ARRAY_ITEMS) {
      result.push(`[${value.length - MAX_ARRAY_ITEMS} more items]`);
    }
    return result;
  }

  const entries = Object.entries(value).slice(0, MAX_ENTRIES);
  const result: Record<string, unknown> = {};
  for (const [key, item] of entries) {
    result[key] = SENSITIVE_KEY.test(key)
      ? '[redacted]'
      : sanitizedValue(item, depth + 1, seen);
  }
  if (Object.keys(value).length > MAX_ENTRIES) {
    result.__truncated__ = `${
      Object.keys(value).length - MAX_ENTRIES
    } more fields`;
  }
  return result;
}

function serializeDetails(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') return clippedString(value);

  let serialized: string;
  try {
    serialized = JSON.stringify(
      sanitizedValue(value, 0, new WeakSet<object>()),
      null,
      2,
    );
  } catch {
    serialized = String(value);
  }
  return serialized.length <= MAX_SERIALIZED_LENGTH
    ? serialized
    : `${serialized.slice(0, MAX_SERIALIZED_LENGTH)}\n…[truncated]`;
}

export function normalizeToastError(
  error: unknown,
  fallback: string,
): {
  summary: string;
  details: string | null;
  copyText: string;
} {
  const record = asRecord(error);
  const summary = (
    error instanceof Error && error.message.trim()
      ? error.message
      : typeof error === 'string' && error.trim()
        ? error
        : fallback
  ).trim();
  const detailParts: string[] = [];

  const code = record?.code ?? record?.error_code;
  if (typeof code === 'string' && code.trim()) {
    detailParts.push(`Code: ${code.trim()}`);
  }
  const status = record?.status ?? record?.httpStatus ?? record?.http_status;
  if (typeof status === 'number' || typeof status === 'string') {
    detailParts.push(`Status: ${String(status)}`);
  }
  const retryable = record?.retryable;
  if (typeof retryable === 'boolean') {
    detailParts.push(`Retryable: ${retryable ? 'yes' : 'no'}`);
  }
  const nextAction = record?.nextAction ?? record?.next_action;
  if (typeof nextAction === 'string' && nextAction.trim()) {
    detailParts.push(`Next action: ${nextAction.trim()}`);
  }
  const details = serializeDetails(record?.details);
  if (details) detailParts.push(`Details:\n${details}`);

  const detailText = detailParts.length > 0 ? detailParts.join('\n') : null;
  return {
    summary,
    details: detailText,
    copyText: detailText ? `${summary}\n\n${detailText}` : summary,
  };
}

export function isPulseErrorMessage(
  value: unknown,
): value is ReactElement<PulseErrorMessageProps> {
  return isValidElement<PulseErrorMessageProps>(value)
    && value.type === PulseErrorMessage;
}

export function showErrorToast(
  error: unknown,
  fallback = 'Something went wrong',
): string {
  const normalized = normalizeToastError(error, fallback);
  if (!normalized.details) return toast.error(normalized.summary);

  return toast.error(createElement(PulseErrorMessage, {
    summary: normalized.summary,
    details: normalized.details,
    copyText: normalized.copyText,
  }));
}
