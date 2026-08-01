import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BoardSettingsForm } from '../BoardSettingsForm';
import type { BoardSettings } from '@/types';

const baseSettings: BoardSettings = {
  max_scenarios_per_card: 2,
  skip_test_coverage_global: false,
  skip_rules_coverage_global: false,
  skip_trs_coverage_global: false,
  skip_contract_coverage_global: false,
  skip_ir_coverage_global: false,
  skip_or_coverage_global: false,
  skip_decisions_coverage_global: false,
  require_task_validation: false,
  min_confidence: 70,
  min_completeness: 70,
  max_drift: 30,
};

describe('BoardSettingsForm — requirement lint languages', () => {
  it('renders the five supported languages with none selected by default', () => {
    render(<BoardSettingsForm settings={baseSettings} onChange={vi.fn()} />);
    const group = screen.getByTestId('lint-languages');
    expect(group.querySelectorAll('button')).toHaveLength(5);
    for (const code of ['pt-BR', 'en-US', 'es-ES', 'de-DE', 'fr-FR']) {
      expect(screen.getByTestId(`lint-language-${code}`)).toHaveAttribute(
        'aria-pressed',
        'false',
      );
    }
  });

  it('selecting a language patches lint_languages with the added code', () => {
    const onChange = vi.fn();
    render(<BoardSettingsForm settings={baseSettings} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('lint-language-de-DE'));
    expect(onChange).toHaveBeenCalledWith({ lint_languages: ['de-DE'] });
  });

  it('deselecting a language removes only that code, preserving order', () => {
    const onChange = vi.fn();
    render(
      <BoardSettingsForm
        settings={{ ...baseSettings, lint_languages: ['pt-BR', 'de-DE'] }}
        onChange={onChange}
      />,
    );
    expect(screen.getByTestId('lint-language-pt-BR')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent.click(screen.getByTestId('lint-language-pt-BR'));
    expect(onChange).toHaveBeenCalledWith({ lint_languages: ['de-DE'] });
  });
});
