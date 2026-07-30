import { describe, expect, it } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import { normalizeToastError } from '@/lib/toastError';

describe('normalizeToastError', () => {
  it('preserves useful structured metadata while redacting sensitive values', () => {
    const error = new AuthenticatedFetchError({
      message: 'The board update was rejected',
      status: 409,
      code: 'version_conflict',
      retryable: true,
      details: {
        expected_version: 8,
        current_version: 9,
        access_token: 'must-not-leak',
        nested: {
          password: 'also-secret',
          reason: 'The entity changed concurrently',
        },
      },
    });

    const normalized = normalizeToastError(error, 'Failed to update board');

    expect(normalized.summary).toBe('The board update was rejected');
    expect(normalized.details).toContain('Code: version_conflict');
    expect(normalized.details).toContain('Status: 409');
    expect(normalized.details).toContain('Retryable: yes');
    expect(normalized.details).toContain('"expected_version": 8');
    expect(normalized.details).toContain('"access_token": "[redacted]"');
    expect(normalized.details).toContain('"password": "[redacted]"');
    expect(normalized.details).not.toContain('must-not-leak');
    expect(normalized.details).not.toContain('also-secret');
    expect(normalized.copyText).toContain(normalized.summary);
    expect(normalized.copyText).toContain(normalized.details);
  });

  it('keeps ordinary errors concise and uses the fallback for unknown values', () => {
    expect(
      normalizeToastError(new Error('Name is required'), 'Save failed'),
    ).toEqual({
      summary: 'Name is required',
      details: null,
      copyText: 'Name is required',
    });
    expect(normalizeToastError(null, 'Save failed')).toEqual({
      summary: 'Save failed',
      details: null,
      copyText: 'Save failed',
    });
  });

  it('limits cyclic and oversized detail payloads without including a stack', () => {
    const details: Record<string, unknown> = {
      note: 'x'.repeat(900),
    };
    details.self = details;
    const error = Object.assign(new Error('Request failed'), {
      details,
      stack: 'sensitive internal stack',
    });

    const normalized = normalizeToastError(error, 'Fallback');

    expect(normalized.details).toContain('[circular]');
    expect(normalized.details).toContain('…');
    expect(normalized.details).not.toContain('sensitive internal stack');
  });
});
