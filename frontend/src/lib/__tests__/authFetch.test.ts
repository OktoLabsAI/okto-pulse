/**
 * Tests for authFetch detail-object branch (FR2 / AC2 / AC3) and getErrorMessage helper.
 *
 * AC2 — detail objects {message} and {error} surface as readable strings, not raw JSON.
 * AC3 — behaviour is status-agnostic: a 409 and a 422 with an object detail both work.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthenticatedFetch } from '../authFetch';
import { getErrorMessage } from '../getErrorMessage';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Builds a minimal Response-like object that AuthenticatedFetch.fetchJson reads */
function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `Status ${status}`,
    json: vi.fn().mockResolvedValue(body),
    headers: new Headers(),
  } as unknown as Response;
}

/** Returns an AuthenticatedFetch whose underlying fetch is replaced by a stub */
function makeClient(response: Response) {
  const tokenGetter = vi.fn().mockResolvedValue('test-token');
  const client = new AuthenticatedFetch(tokenGetter, '');

  // Patch the internal fetch call so no real network is used
  vi.spyOn(global, 'fetch').mockResolvedValue(response);

  return client;
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// AC2 — detail object shape extraction
// ---------------------------------------------------------------------------

describe('authFetch – detail-object branch (AC2)', () => {
  it('surfaces detail.message when detail is {message: "X"}', async () => {
    const response = fakeResponse(400, { detail: { message: 'Conflict on field X' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('Conflict on field X');
  });

  it('surfaces detail.error when detail is {error: "Y"} and no message key', async () => {
    const response = fakeResponse(400, { detail: { error: 'Duplicate entry Y' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('Duplicate entry Y');
  });

  it('prefers detail.message over detail.error when both are present', async () => {
    const response = fakeResponse(400, { detail: { message: 'Primary msg', error: 'secondary' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('Primary msg');
  });

  it('falls back to JSON.stringify when detail object has neither message nor error', async () => {
    const response = fakeResponse(400, { detail: { code: 'ERR_42', reason: 'unknown' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow(
      JSON.stringify({ code: 'ERR_42', reason: 'unknown' }),
    );
  });

  it('passes through a string detail unchanged', async () => {
    const response = fakeResponse(400, { detail: 'plain string detail' });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('plain string detail');
  });

  it('does NOT produce raw JSON for a {message} detail (regression guard)', async () => {
    const response = fakeResponse(400, { detail: { message: 'Must not be raw JSON' } });
    const client = makeClient(response);

    try {
      await client.fetchJson('/api/test');
      expect.fail('should have thrown');
    } catch (err) {
      const msg = (err as Error).message;
      // must NOT look like a JSON string that starts with "{"
      expect(msg.startsWith('{')).toBe(false);
      expect(msg).toBe('Must not be raw JSON');
    }
  });
});

// ---------------------------------------------------------------------------
// AC3 — status-agnostic: any 4xx carries the detail message
// ---------------------------------------------------------------------------

describe('authFetch – status-agnostic 4xx (AC3)', () => {
  it('extracts detail.message from a 409 Conflict response', async () => {
    const response = fakeResponse(409, { detail: { message: 'Resource already exists' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('Resource already exists');
  });

  it('extracts detail.message from a 422 Unprocessable Entity response', async () => {
    const response = fakeResponse(422, { detail: { message: 'Validation failed for field foo' } });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow(
      'Validation failed for field foo',
    );
  });

  it('extracts string detail from a 403 Forbidden response', async () => {
    const response = fakeResponse(403, { detail: 'Access denied' });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('Access denied');
  });

  it('uses HTTP status fallback when no detail present (404)', async () => {
    const response = fakeResponse(404, {});
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('HTTP 404');
  });
});

// ---------------------------------------------------------------------------
// BFF backend_error proxy pattern
// ---------------------------------------------------------------------------

describe('authFetch – backend_error.detail proxy pattern', () => {
  it('extracts message from backend_error.detail object', async () => {
    const response = fakeResponse(400, {
      backend_error: { detail: { message: 'from BFF proxy' } },
    });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('from BFF proxy');
  });

  it('backend_error.detail string passes through', async () => {
    const response = fakeResponse(400, {
      backend_error: { detail: 'proxy string detail' },
    });
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).rejects.toThrow('proxy string detail');
  });
});

// ---------------------------------------------------------------------------
// Top-level structured error (no `detail`) — human message wins over machine code
// ---------------------------------------------------------------------------

describe('authFetch – top-level structured error without detail', () => {
  it('prefers errorData.message over errorData.error when there is no detail (BUG 3 teeth)', async () => {
    // DesignSystemError-style payload: structured fields at the TOP level, no `detail`.
    const response = fakeResponse(422, {
      error: 'design_system_required',
      code: 'design_system_required',
      message: 'Design System reference is required',
      status_code: 422,
    });
    const client = makeClient(response);
    // the human-readable message must win — NOT the machine code.
    await expect(client.fetchJson('/api/test')).rejects.toThrow('Design System reference is required');
  });

  it('does NOT surface the machine code when a human message exists (regression guard)', async () => {
    const response = fakeResponse(422, {
      error: 'design_system_required',
      message: 'Design System reference is required',
    });
    const client = makeClient(response);
    try {
      await client.fetchJson('/api/test');
      expect.fail('should have thrown');
    } catch (err) {
      expect((err as Error).message).toBe('Design System reference is required');
      expect((err as Error).message).not.toBe('design_system_required');
    }
  });

  it('falls back to errorData.error when there is no message and no detail', async () => {
    const response = fakeResponse(422, { error: 'only_a_code' });
    const client = makeClient(response);
    await expect(client.fetchJson('/api/test')).rejects.toThrow('only_a_code');
  });
});

// ---------------------------------------------------------------------------
// Empty-body success responses (204 No Content / DELETE) — must not throw on parse
// ---------------------------------------------------------------------------

/** Success-path Response stub exposing text(); json() rejects to prove it is not used. */
function fakeOkResponse(status: number, text: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `Status ${status}`,
    json: vi.fn().mockRejectedValue(new SyntaxError('Unexpected end of JSON input')),
    text: vi.fn().mockResolvedValue(text),
    headers: new Headers(),
  } as unknown as Response;
}

describe('authFetch – empty-body success (204 / DELETE)', () => {
  it('returns undefined for a 204 No Content without calling json() (unlink teeth)', async () => {
    const response = fakeOkResponse(204, '');
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test', { method: 'DELETE' })).resolves.toBeUndefined();
    // json() on an empty body throws "Unexpected end of JSON input" — it must NOT run.
    expect(response.json).not.toHaveBeenCalled();
  });

  it('returns undefined for a 200 with an empty body', async () => {
    const response = fakeOkResponse(200, '');
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).resolves.toBeUndefined();
  });

  it('still parses a normal JSON body on success', async () => {
    const response = fakeOkResponse(200, JSON.stringify({ ok: true, n: 7 }));
    const client = makeClient(response);

    await expect(client.fetchJson('/api/test')).resolves.toEqual({ ok: true, n: 7 });
  });
});

// ---------------------------------------------------------------------------
// getErrorMessage helper
// ---------------------------------------------------------------------------

describe('getErrorMessage', () => {
  it('returns err.message for an Error instance', () => {
    expect(getErrorMessage(new Error('hello'))).toBe('hello');
  });

  it('returns the string as-is when err is a string', () => {
    expect(getErrorMessage('direct string')).toBe('direct string');
  });

  it('returns fallback for null', () => {
    expect(getErrorMessage(null)).toBe('Something went wrong');
  });

  it('returns fallback for undefined', () => {
    expect(getErrorMessage(undefined)).toBe('Something went wrong');
  });

  it('returns fallback for a number', () => {
    expect(getErrorMessage(42)).toBe('Something went wrong');
  });

  it('returns fallback for a plain object', () => {
    expect(getErrorMessage({ code: 500 })).toBe('Something went wrong');
  });
});
