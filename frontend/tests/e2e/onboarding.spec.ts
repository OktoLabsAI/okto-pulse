/**
 * OnboardingModal — Playwright e2e covering TS-1, TS-2, TS-7, TS-10.
 *
 * The modal mounts after Terms of Use is accepted (or pre-accepted via
 * URL flag `?accept_terms=1`). Tests use the URL flag to bypass the
 * terms scroll-to-end friction.
 *
 * Scenarios:
 *   TS-1 — first-time user accepts terms and immediately sees slide 1
 *   TS-2 — returning user (flag set) does NOT see the modal on reload
 *   TS-7 — Right + Right + Esc keyboard nav advances slides and closes
 *   TS-10 — every dismissal path sets `okto.onboarding.completed.v1=true`
 *           and dispatches the `okto:onboarding-completed` event
 */

import { expect, test } from '@playwright/test';

const FLAG = 'okto.onboarding.completed.v1';
const TERMS_URL = '/?accept_terms=1';

async function clearStorage(page) {
  await page.goto('/');
  await page.evaluate(() => localStorage.clear());
}

test.describe('OnboardingModal — TS-1 first-time user', () => {
  test('mounts on slide 1 immediately after Terms accept', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    const modal = page.getByTestId('onboarding-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toHaveAttribute('data-slide', '1');
    await expect(page.getByText('Welcome to')).toBeVisible();
  });
});

test.describe('OnboardingModal — TS-2 returning user', () => {
  test('does NOT mount when the completion flag is set', async ({ page }) => {
    await page.goto('/');
    await page.evaluate((key) => localStorage.setItem(key, 'true'), FLAG);
    await page.goto(TERMS_URL);
    // Give the React effects a moment to settle.
    await page.waitForTimeout(300);
    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);
  });
});

test.describe('OnboardingModal — TS-7 keyboard navigation', () => {
  test('Right + Right + Esc advances to slide 3 then closes with flag set', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    const modal = page.getByTestId('onboarding-modal');
    await expect(modal).toBeVisible();

    await page.keyboard.press('ArrowRight');
    await expect(modal).toHaveAttribute('data-slide', '2');

    await page.keyboard.press('ArrowRight');
    await expect(modal).toHaveAttribute('data-slide', '3');

    await page.keyboard.press('Escape');
    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);

    const flag = await page.evaluate((key) => localStorage.getItem(key), FLAG);
    expect(flag).toBe('true');
  });
});

test.describe('OnboardingModal — TS-10 dismissal paths', () => {
  test('"Get started" CTA on slide 3 sets flag + fires CustomEvent', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    await page.evaluate(() => {
      (window as any).__onboardingEvents = 0;
      window.addEventListener(
        'okto:onboarding-completed',
        () => ((window as any).__onboardingEvents += 1),
      );
    });
    await page.getByTestId('onboarding-primary-cta').click(); // -> slide 2
    await page.getByTestId('onboarding-primary-cta').click(); // -> slide 3
    await page.getByTestId('onboarding-primary-cta').click(); // Get started

    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);
    const flag = await page.evaluate((key) => localStorage.getItem(key), FLAG);
    expect(flag).toBe('true');
    const fired = await page.evaluate(() => (window as any).__onboardingEvents);
    expect(fired).toBe(1);
  });

  test('Close (X) button sets flag + fires CustomEvent', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    await page.evaluate(() => {
      (window as any).__onboardingEvents = 0;
      window.addEventListener(
        'okto:onboarding-completed',
        () => ((window as any).__onboardingEvents += 1),
      );
    });
    await page.getByTestId('onboarding-close-button').click();
    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);
    expect(
      await page.evaluate((key) => localStorage.getItem(key), FLAG),
    ).toBe('true');
    expect(await page.evaluate(() => (window as any).__onboardingEvents)).toBe(1);
  });

  test('Esc key sets flag + fires CustomEvent', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    await page.evaluate(() => {
      (window as any).__onboardingEvents = 0;
      window.addEventListener(
        'okto:onboarding-completed',
        () => ((window as any).__onboardingEvents += 1),
      );
    });
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);
    expect(
      await page.evaluate((key) => localStorage.getItem(key), FLAG),
    ).toBe('true');
    expect(await page.evaluate(() => (window as any).__onboardingEvents)).toBe(1);
  });

  test('Backdrop click sets flag + fires CustomEvent', async ({ page }) => {
    await clearStorage(page);
    await page.goto(TERMS_URL);
    await page.evaluate(() => {
      (window as any).__onboardingEvents = 0;
      window.addEventListener(
        'okto:onboarding-completed',
        () => ((window as any).__onboardingEvents += 1),
      );
    });
    // Click on the backdrop area (outside the inner card) — top-left of viewport.
    const modal = page.getByTestId('onboarding-modal');
    const box = await modal.boundingBox();
    if (!box) throw new Error('modal not visible for backdrop test');
    await page.mouse.click(box.x + 5, box.y + 5);
    await expect(page.getByTestId('onboarding-modal')).toHaveCount(0);
    expect(
      await page.evaluate((key) => localStorage.getItem(key), FLAG),
    ).toBe('true');
    expect(await page.evaluate(() => (window as any).__onboardingEvents)).toBe(1);
  });
});
