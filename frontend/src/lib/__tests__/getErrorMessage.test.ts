
import { describe, expect, it } from 'vitest';
import { getErrorMessage } from '../getErrorMessage';

describe('getErrorMessage', () => {
  it('returns the message from an Error', () => {
    expect(getErrorMessage(new Error('Login failed'))).toBe('Login failed');
  });

  it('handles Error subclasses', () => {
    class CustomError extends Error {}
    expect(getErrorMessage(new CustomError('Custom error'))).toBe('Custom error');
  });

  it('returns a string unchanged', () => {
    expect(getErrorMessage('Connection lost')).toBe('Connection lost');
  });

  it('returns an empty string unchanged', () => {
    expect(getErrorMessage('')).toBe('');
  });

  it('uses the fallback for null', () => {
    expect(getErrorMessage(null)).toBe('Something went wrong');
  });

  it('uses the fallback for undefined', () => {
    expect(getErrorMessage(undefined)).toBe('Something went wrong');
  });

  it('uses the fallback for numbers', () => {
    expect(getErrorMessage(404)).toBe('Something went wrong');
  });

  it('returns the fallback for non-Error objects', () => {
    expect(getErrorMessage({ message: 'Failed' })).toBe('Something went wrong');
  });
});
