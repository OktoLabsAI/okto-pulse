/**
 * Sprint C1 — TC-1, TC-2, TC-3, TC-5
 *
 * Combined vitest suite for the terms acceptance flow:
 * - TC-1: modal renders and acts as a blocker
 * - TC-2: scroll-to-end enables Accept; click persists localStorage
 * - TC-3: re-prompt when stored hash diverges from current TERMS_HASH
 * - TC-5: aggregated suite (this file)
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, act } from '@testing-library/react';
import { TermsAcceptanceModal } from '../TermsAcceptanceModal';
import { renderHook } from '@testing-library/react';
import { useTermsAcceptance } from '@/hooks/useTermsAcceptance';
import { saveAcceptance, clearAcceptance, loadAcceptance } from '@/services/terms-acceptance-api';
import { TERMS_HASH } from '@/constants/terms';

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = '';
  // jsdom doesn't compute layout; force scrollHeight so handleScroll can fire.
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get() { return 1000; },
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() { return 400; },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('TermsAcceptanceModal — TC-1 + TC-2', () => {
  it('TC-1: renders the modal with title and disabled accept button', () => {
    render(<TermsAcceptanceModal onAccept={() => {}} />);
    expect(screen.getByTestId('terms-acceptance-modal')).toBeTruthy();
    const btn = screen.getByTestId('terms-modal-accept') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('TC-2: scrolling to end enables the accept button and click invokes onAccept', () => {
    const onAccept = vi.fn();
    render(<TermsAcceptanceModal onAccept={onAccept} />);
    const scroller = screen.getByTestId('terms-modal-scroll');
    // Simulate reaching the bottom
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get() { return 600; } });
    fireEvent.scroll(scroller);
    const btn = screen.getByTestId('terms-modal-accept') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    expect(onAccept).toHaveBeenCalledTimes(1);
  });

  it('TC-2: saveAcceptance writes a record to localStorage with current hash', () => {
    const rec = saveAcceptance('modal');
    expect(rec.source).toBe('modal');
    expect(rec.hash).toBe(TERMS_HASH);
    const persisted = loadAcceptance();
    expect(persisted).not.toBeNull();
    expect(persisted!.hash).toBe(TERMS_HASH);
  });
});

describe('useTermsAcceptance — TC-3 (re-prompt on hash mismatch)', () => {
  it('treats a stored record with a mismatched hash as missing (re-prompt)', async () => {
    // Pre-populate with a stale hash from a previous version
    localStorage.setItem(
      'okto.terms.acceptance',
      JSON.stringify({
        accepted_at: '2026-01-01T00:00:00Z',
        version: '0.0.0',
        hash: 'old-hash-different-from-current',
        source: 'modal',
      }),
    );
    // Stub fetch so the bootstrap doesn't try the backend
    const fetchSpy = vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ terms_acceptance: { pre_accepted: false, source: null } }),
    } as any);

    const { result } = renderHook(() => useTermsAcceptance());
    // Wait for the async bootstrap
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(result.current.needsAcceptance).toBe(true);
    expect(result.current.acceptance).toBeNull();
    fetchSpy.mockRestore();
  });

  it('honors a fresh cached acceptance when hash matches', async () => {
    saveAcceptance('modal');
    const { result } = renderHook(() => useTermsAcceptance());
    await act(async () => { await Promise.resolve(); });
    expect(result.current.needsAcceptance).toBe(false);
    expect(result.current.acceptance).not.toBeNull();
    expect(result.current.acceptance!.hash).toBe(TERMS_HASH);
  });

  it('honors backend pre-acceptance via /api/v1/me/system-flags', async () => {
    clearAcceptance();
    const fetchSpy = vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        terms_acceptance: { pre_accepted: true, source: 'env', record: null, current_hash: TERMS_HASH },
      }),
    } as any);
    const { result } = renderHook(() => useTermsAcceptance());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.needsAcceptance).toBe(false);
    expect(result.current.acceptance?.source).toBe('env');
    fetchSpy.mockRestore();
  });

  it('honors window.OKTO_PULSE_TERMS_ACCEPTED=1 (env injection)', async () => {
    clearAcceptance();
    (window as any).OKTO_PULSE_TERMS_ACCEPTED = '1';
    const { result } = renderHook(() => useTermsAcceptance());
    await act(async () => { await Promise.resolve(); });
    expect(result.current.needsAcceptance).toBe(false);
    expect(result.current.acceptance?.source).toBe('env');
    delete (window as any).OKTO_PULSE_TERMS_ACCEPTED;
  });

  it('honors ?accept_terms=1 URL param (CLI redirector) and removes it from the URL', async () => {
    clearAcceptance();
    window.history.replaceState({}, '', '/?accept_terms=1');
    const { result } = renderHook(() => useTermsAcceptance());
    await act(async () => { await Promise.resolve(); });
    expect(result.current.needsAcceptance).toBe(false);
    expect(result.current.acceptance?.source).toBe('cli');
    expect(window.location.search).not.toContain('accept_terms');
  });
});
