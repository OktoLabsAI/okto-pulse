/**
 * ScenarioTypeBadge — supported enum + historical-invalid reporting
 * (spec ac16b3c9, IMP card bf52c32f).
 *
 * The badge advertises ONLY unit/integration/e2e/manual and renders any
 * persisted value outside that enum EXPLICITLY as `<value> (unsupported)` with a
 * warning marker + tooltip — so a stale value like `regression`/`negative` is
 * surfaced for remediation instead of being shown as a plain supported label.
 */

import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import {
  ScenarioTypeBadge,
  SCENARIO_TYPES,
  isSupportedScenarioType,
} from '../ScenarioTypeBadge';

afterEach(() => cleanup());

describe('ScenarioTypeBadge', () => {
  it('advertises exactly unit/integration/e2e/manual and nothing else', () => {
    expect([...SCENARIO_TYPES]).toEqual(['unit', 'integration', 'e2e', 'manual']);
    expect(isSupportedScenarioType('negative')).toBe(false);
    expect(isSupportedScenarioType('regression')).toBe(false);
  });

  it('renders each supported type plainly, with no unsupported marker', () => {
    for (const t of SCENARIO_TYPES) {
      const { unmount } = render(<ScenarioTypeBadge scenarioType={t} />);
      const badge = screen.getByTestId('scenario-type-badge');
      expect(badge.textContent).toBe(t);
      expect(badge.getAttribute('data-unsupported')).toBeNull();
      expect(badge.getAttribute('title')).toBeNull();
      unmount();
    }
  });

  it('reports a historical/invalid persisted type EXPLICITLY (not silently coerced)', () => {
    render(<ScenarioTypeBadge scenarioType="regression" />);
    const badge = screen.getByTestId('scenario-type-badge');
    expect(badge.textContent).toBe('regression (unsupported)');
    expect(badge.getAttribute('data-unsupported')).toBe('true');
    const title = badge.getAttribute('title') ?? '';
    expect(title).toContain('Unsupported scenario_type');
    expect(title).toContain('unit, integration, e2e, manual');
    // the invalid value is NOT relabelled as a supported type — intent preserved.
    expect(badge.textContent).not.toBe('integration');
  });
});
