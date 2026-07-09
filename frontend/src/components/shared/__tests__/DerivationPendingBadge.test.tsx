import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  DerivationPendingBadge,
  getIdeationPendingDerivationLabel,
  getRefinementPendingDerivationLabel,
  IDEATION_PENDING_REFINEMENT_LABEL,
  REFINEMENT_PENDING_SPEC_LABEL,
} from '../DerivationPendingBadge';

describe('derivation pending badge helpers', () => {
  it('shows No refinement only for done medium/large ideations without active refinements', () => {
    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'medium',
        active_refinement_count: 0,
      }),
    ).toBe(IDEATION_PENDING_REFINEMENT_LABEL);

    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'large',
        refinements: [
          { status: 'cancelled', archived: false },
          { status: 'draft', archived: true },
        ],
      }),
    ).toBe(IDEATION_PENDING_REFINEMENT_LABEL);

    expect(
      getIdeationPendingDerivationLabel({
        status: 'review',
        complexity: 'medium',
        active_refinement_count: 0,
      }),
    ).toBeNull();
    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'medium',
        refinements: [{ status: 'draft', archived: false }],
      }),
    ).toBeNull();
  });

  it('shows No spec for done small ideations without active direct specs', () => {
    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'small',
        active_spec_count: 0,
      }),
    ).toBe(REFINEMENT_PENDING_SPEC_LABEL);

    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'small',
        specs: [
          { status: 'cancelled', archived: false, refinement_id: null },
          { status: 'draft', archived: true, refinement_id: null },
        ],
      }),
    ).toBe(REFINEMENT_PENDING_SPEC_LABEL);

    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'small',
        specs: [{ status: 'draft', archived: false, refinement_id: null }],
      }),
    ).toBeNull();

    expect(
      getIdeationPendingDerivationLabel({
        status: 'done',
        complexity: 'small',
        specs: [{ status: 'draft', archived: false, refinement_id: 'ref-1' }],
      }),
    ).toBe(REFINEMENT_PENDING_SPEC_LABEL);
  });

  it('shows No spec only for done refinements without active specs', () => {
    expect(
      getRefinementPendingDerivationLabel({
        status: 'done',
        active_spec_count: 0,
      }),
    ).toBe(REFINEMENT_PENDING_SPEC_LABEL);

    expect(
      getRefinementPendingDerivationLabel({
        status: 'done',
        specs: [
          { status: 'cancelled', archived: false },
          { status: 'draft', archived: true },
        ],
      }),
    ).toBe(REFINEMENT_PENDING_SPEC_LABEL);

    expect(
      getRefinementPendingDerivationLabel({
        status: 'approved',
        active_spec_count: 0,
      }),
    ).toBeNull();
    expect(
      getRefinementPendingDerivationLabel({
        status: 'done',
        active_spec_count: 1,
      }),
    ).toBeNull();
    expect(
      getRefinementPendingDerivationLabel({
        status: 'done',
        specs: [{ status: 'draft', archived: false }],
      }),
    ).toBeNull();
  });
});

describe('DerivationPendingBadge component', () => {
  it('renders the actionable label as a status badge', () => {
    render(<DerivationPendingBadge label={IDEATION_PENDING_REFINEMENT_LABEL} />);

    const badge = screen.getByTestId('derivation-pending-badge');
    expect(badge).toHaveTextContent(IDEATION_PENDING_REFINEMENT_LABEL);
    expect(badge).toHaveAttribute('role', 'status');
  });

  it('renders nothing without a label', () => {
    const { container } = render(<DerivationPendingBadge label={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('derivation pending badge panel wiring', () => {
  it('uses the shared helper in list/detail surfaces without per-card refinement fetches', () => {
    const ideationsPanel = readFileSync(
      join(process.cwd(), 'src/components/ideations/IdeationsPanel.tsx'),
      'utf8',
    );
    const ideationModal = readFileSync(
      join(process.cwd(), 'src/components/ideations/IdeationModal.tsx'),
      'utf8',
    );
    const refinementsPanel = readFileSync(
      join(process.cwd(), 'src/components/refinements/RefinementsPanel.tsx'),
      'utf8',
    );
    const refinementModal = readFileSync(
      join(process.cwd(), 'src/components/refinements/RefinementModal.tsx'),
      'utf8',
    );

    expect(ideationsPanel).toContain('getIdeationPendingDerivationLabel(ideation)');
    expect(ideationsPanel).toContain('ideations-no-derivation-filter');
    expect(ideationsPanel).toContain('derivationFilteredIdeations');
    expect(ideationModal).toContain('getIdeationPendingDerivationLabel(ideation)');
    expect(refinementsPanel).toContain('getRefinementPendingDerivationLabel(refinement)');
    expect(refinementsPanel).toContain('refinements-no-derivation-filter');
    expect(refinementsPanel).toContain('statusFilteredRefinements.filter(({ refinement }) => Boolean(getRefinementPendingDerivationLabel(refinement)))');
    expect(refinementModal).toContain('getRefinementPendingDerivationLabel(refinement)');
    expect(refinementsPanel).not.toContain('api.getRefinement(refinement.id)');
  });
});
