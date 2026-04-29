/**
 * Vitest suite for the OnboardingModal — covers the unit + integration
 * scenarios that don't need a real browser:
 *   TS-3, TS-4, TS-6, TS-8, TS-9, TS-11, TS-12.
 *
 * E2E scenarios (TS-1, TS-2, TS-7, TS-10) live in
 * `frontend/tests/e2e/onboarding.spec.ts`. The manual contrast audit (TS-5)
 * is tracked in card #188.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, act } from '@testing-library/react';
import fs from 'node:fs';
import path from 'node:path';
import { OnboardingModal } from '../OnboardingModal';
import { onboardingStorage } from '../onboardingStorage';

// jsdom does not implement matchMedia by default — useTheme reads it.
beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = '';
  document.body.innerHTML = '';
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
  }
});

afterEach(() => {
  onboardingStorage.reset();
});

function open(props: Partial<React.ComponentProps<typeof OnboardingModal>> = {}) {
  return render(<OnboardingModal onClose={props.onClose ?? (() => {})} mcpUrl={props.mcpUrl} />);
}

describe('OnboardingModal — TS-3 (slide navigation)', () => {
  it('renders slides 1, 2, 3 in order via Next, then dot indicator follows', () => {
    open();
    // slide 1
    expect(screen.getByTestId('onboarding-modal').getAttribute('data-slide')).toBe('1');
    expect(screen.getByText('Welcome to')).toBeTruthy();
    expect(screen.getByTestId('onboarding-dot-1').getAttribute('data-active')).toBe('true');

    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    expect(screen.getByTestId('onboarding-modal').getAttribute('data-slide')).toBe('2');
    expect(screen.getByTestId('onboarding-dot-2').getAttribute('data-active')).toBe('true');

    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    expect(screen.getByTestId('onboarding-modal').getAttribute('data-slide')).toBe('3');
    expect(screen.getByTestId('onboarding-dot-3').getAttribute('data-active')).toBe('true');
  });

  it('Back button is hidden on slide 1 and visible on slides 2/3', () => {
    open();
    expect(screen.queryByTestId('onboarding-back-button')).toBeNull();
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    expect(screen.getByTestId('onboarding-back-button')).toBeTruthy();
    fireEvent.click(screen.getByTestId('onboarding-back-button'));
    expect(screen.getByTestId('onboarding-modal').getAttribute('data-slide')).toBe('1');
  });
});

describe('OnboardingModal — TS-4 (theme switch toggles global theme)', () => {
  it('Click on theme toggle updates the documentElement class', () => {
    document.documentElement.classList.remove('dark');
    open();
    const wasDark = document.documentElement.classList.contains('dark');
    fireEvent.click(screen.getByTestId('onboarding-theme-toggle'));
    const isDark = document.documentElement.classList.contains('dark');
    expect(isDark).toBe(!wasDark);
  });
});

describe('OnboardingModal — TS-6 (English-only copy)', () => {
  it('source files contain no Portuguese tokens (denylist scan)', () => {
    const dir = path.resolve(__dirname, '..');
    const files = [
      'OnboardingModal.tsx',
      'WelcomeSlide.tsx',
      'QuickStartSlide.tsx',
      'AssistantBindingSlide.tsx',
    ];
    // Minimal, conservative denylist — words that appear ONLY in Portuguese
    // copy. We avoid English false positives (e.g. "para", "you").
    const denylist = [
      /\bnão\b/i,
      /\bsão\b/i,
      /\bpara\b/i,
      /\bagente\b/i,
      /\bbem-vindo\b/i,
      /\bcomeçar\b/i,
      /\bconfigurar\b/i,
      /\bcópia\b/i,
      /\bçã/i, // ã + ç combo, very Portuguese
    ];
    const offenders: string[] = [];
    for (const file of files) {
      const src = fs.readFileSync(path.join(dir, file), 'utf-8');
      for (const re of denylist) {
        if (re.test(src)) offenders.push(`${file}: matched ${re}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe('OnboardingModal — TS-8 (focus trap)', () => {
  it('Tab from the last focusable wraps to the first', () => {
    open();
    const modal = screen.getByTestId('onboarding-modal');
    const focusables = modal.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    expect(first).toBeTruthy();
    expect(last).toBeTruthy();

    last.focus();
    fireEvent.keyDown(modal, { key: 'Tab' });
    expect(document.activeElement).toBe(first);
  });

  it('Shift+Tab from the first focusable wraps to the last', () => {
    open();
    const modal = screen.getByTestId('onboarding-modal');
    const focusables = modal.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    first.focus();
    fireEvent.keyDown(modal, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
  });
});

describe('OnboardingModal — TS-9 (Copy MCP URL writes to clipboard)', () => {
  it('clicking Copy invokes navigator.clipboard.writeText with the URL and flips label', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    open({ mcpUrl: 'http://127.0.0.1:8101/mcp?api_key=dash_unit-test' });
    fireEvent.click(screen.getByTestId('onboarding-primary-cta')); // -> slide 2
    fireEvent.click(screen.getByTestId('onboarding-primary-cta')); // -> slide 3

    const copyBtn = screen.getByTestId('onboarding-copy-button');
    expect(copyBtn.textContent).toBe('Copy');

    await act(async () => {
      fireEvent.click(copyBtn);
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledWith('http://127.0.0.1:8101/mcp?api_key=dash_unit-test');
    expect(copyBtn.textContent).toBe('Copied!');
  });
});

describe('OnboardingModal — TS-11 (slide 3 CTA reads "Get started")', () => {
  it('CTA label is exactly "Get started" on slide 3 (no arrow)', () => {
    open();
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    const cta = screen.getByTestId('onboarding-primary-cta');
    expect(cta.textContent).toBe('Get started');
  });
});

describe('OnboardingModal — TS-12 (dot indicator updates with active slide)', () => {
  it('only the dot for the current slide carries data-active="true"', () => {
    open();
    expect(screen.getByTestId('onboarding-dot-1').getAttribute('data-active')).toBe('true');
    expect(screen.getByTestId('onboarding-dot-2').getAttribute('data-active')).toBe('false');
    expect(screen.getByTestId('onboarding-dot-3').getAttribute('data-active')).toBe('false');

    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    expect(screen.getByTestId('onboarding-dot-1').getAttribute('data-active')).toBe('false');
    expect(screen.getByTestId('onboarding-dot-2').getAttribute('data-active')).toBe('true');
    expect(screen.getByTestId('onboarding-dot-3').getAttribute('data-active')).toBe('false');
  });
});

describe('OnboardingModal — dismissal paths set the completion flag', () => {
  it('Get started on slide 3 calls onClose and marks completed', () => {
    const onClose = vi.fn();
    open({ onClose });
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    fireEvent.click(screen.getByTestId('onboarding-primary-cta'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onboardingStorage.isCompleted()).toBe(true);
  });

  it('Close (X) calls onClose and marks completed', () => {
    const onClose = vi.fn();
    open({ onClose });
    fireEvent.click(screen.getByTestId('onboarding-close-button'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onboardingStorage.isCompleted()).toBe(true);
  });

  it('Esc key calls onClose and marks completed', () => {
    const onClose = vi.fn();
    open({ onClose });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onboardingStorage.isCompleted()).toBe(true);
  });
});
