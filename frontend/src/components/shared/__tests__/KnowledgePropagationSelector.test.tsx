import { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  KnowledgePropagationSelector,
} from '../KnowledgePropagationSelector';
import type { KnowledgePropagationCandidate } from '../knowledgePropagationCandidates';
import {
  buildKnowledgePropagationEnvelope,
  EMPTY_KNOWLEDGE_PROPAGATION_CHOICE,
  isKnowledgePropagationChoiceValid,
  type KnowledgePropagationChoice,
} from '../knowledgePropagationChoice';

const candidates: KnowledgePropagationCandidate[] = [
  {
    id: 'root-b',
    title: 'Operational notes',
    description: 'Runbook evidence',
    origin_class: 'legacy_all',
  },
  {
    id: 'root-a',
    title: 'API reference',
    description: 'Canonical endpoint notes',
    origin_class: 'v2',
    stale: true,
  },
  {
    id: 'root-c',
    title: 'Selected legacy notes',
    origin_class: 'selected_legacy',
  },
  {
    id: 'root-d',
    title: 'Unresolved legacy notes',
    origin_class: 'legacy_unresolved',
  },
];

function Harness({
  items = candidates,
  initialValue = EMPTY_KNOWLEDGE_PROPAGATION_CHOICE,
  loading = false,
  error = null,
  onRetry,
}: {
  items?: KnowledgePropagationCandidate[];
  initialValue?: KnowledgePropagationChoice;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
}) {
  const [value, setValue] = useState<KnowledgePropagationChoice>(initialValue);
  return (
    <>
      <KnowledgePropagationSelector
        items={items}
        value={value}
        onChange={setValue}
        loading={loading}
        error={error}
        onRetry={onRetry}
      />
      <output data-testid="choice-value">{JSON.stringify(value)}</output>
    </>
  );
}

function currentChoice(): KnowledgePropagationChoice {
  return JSON.parse(screen.getByTestId('choice-value').textContent || '{}');
}

describe('KnowledgePropagationSelector', () => {
  it('starts with an authoritative omitted choice and zero pre-selected resources', () => {
    render(<Harness />);

    expect(screen.getByRole('radio', { name: 'No decision' })).toBeChecked();
    for (const checkbox of screen.getAllByRole('checkbox')) {
      expect(checkbox).not.toBeChecked();
      expect(checkbox).toBeDisabled();
    }
    expect(currentChoice()).toEqual(EMPTY_KNOWLEDGE_PROPAGATION_CHOICE);
    expect(screen.getByText(/Omitted never means “select all”/i)).toBeInTheDocument();
  });

  it('keeps omitted distinct from an explicitly empty drop decision', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('radio', { name: 'Drop' }));

    expect(currentChoice()).toEqual({
      action: 'drop',
      knowledgeIds: [],
      justification: '',
    });
    expect(screen.getByText(/Explicit empty will be saved/i)).toBeInTheDocument();
    expect(isKnowledgePropagationChoiceValid(currentChoice())).toBe(false);

    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: 'This destination must start without inherited Knowledge.' },
    });

    expect(isKnowledgePropagationChoiceValid(currentChoice())).toBe(true);
    expect(
      buildKnowledgePropagationEnvelope(currentChoice(), 'idem-empty'),
    ).toEqual({
      contract_version: 2,
      selection_state: 'explicit_empty',
      mode: 'drop',
      knowledge_ids: [],
      justification: 'This destination must start without inherited Knowledge.',
      idempotency_key: 'idem-empty',
      expected_revision: 0,
      relevance_links: [],
    });
  });

  it('builds explicit_ids with a required mode, justification and canonical root IDs', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('radio', { name: 'Snapshot' }));
    expect(screen.getByText(/Select at least one resource/i)).toBeInTheDocument();
    expect(isKnowledgePropagationChoiceValid(currentChoice())).toBe(false);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Operational notes' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select API reference' }));
    expect(isKnowledgePropagationChoiceValid(currentChoice())).toBe(false);

    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: '  Both references explain the implementation boundary.  ' },
    });

    expect(isKnowledgePropagationChoiceValid(currentChoice())).toBe(true);
    expect(
      buildKnowledgePropagationEnvelope(currentChoice(), 'idem-selected'),
    ).toEqual({
      contract_version: 2,
      selection_state: 'explicit_ids',
      mode: 'snapshot',
      knowledge_ids: ['root-a', 'root-b'],
      justification: 'Both references explain the implementation boundary.',
      idempotency_key: 'idem-selected',
      expected_revision: 0,
      relevance_links: [],
    });
  });

  it('validates every tri-state combination without inferring empty intent', () => {
    expect(isKnowledgePropagationChoiceValid({
      action: 'omitted',
      knowledgeIds: [],
      justification: '',
    })).toBe(true);
    expect(isKnowledgePropagationChoiceValid({
      action: 'reference',
      knowledgeIds: [],
      justification: 'Relevant',
    })).toBe(false);
    expect(isKnowledgePropagationChoiceValid({
      action: 'snapshot',
      knowledgeIds: ['root-a'],
      justification: '   ',
    })).toBe(false);
    expect(isKnowledgePropagationChoiceValid({
      action: 'drop',
      knowledgeIds: [],
      justification: 'Explicitly empty',
    })).toBe(true);

    expect(
      buildKnowledgePropagationEnvelope(
        EMPTY_KNOWLEDGE_PROPAGATION_CHOICE,
        'idem-omitted',
      ),
    ).toEqual({
      contract_version: 2,
      selection_state: 'omitted',
      mode: null,
      knowledge_ids: [],
      justification: null,
      idempotency_key: 'idem-omitted',
      expected_revision: 0,
      relevance_links: [],
    });
  });

  it('renders stale state and every supported origin class as visible badges', () => {
    render(
      <Harness
        initialValue={{
          action: 'reference',
          knowledgeIds: [],
          justification: '',
        }}
      />,
    );

    const selector = screen.getByTestId('knowledge-propagation-selector');
    expect(within(selector).getByText('stale')).toBeInTheDocument();
    expect(within(selector).getByText('v2')).toBeInTheDocument();
    expect(within(selector).getByText('legacy all')).toBeInTheDocument();
    expect(within(selector).getByText('selected legacy')).toBeInTheDocument();
    expect(within(selector).getByText('legacy unresolved')).toBeInTheDocument();
  });

  it('announces loading, empty and error states without conflating them', () => {
    const retry = vi.fn();
    const view = render(<Harness loading />);
    const selector = () => screen.getByTestId('knowledge-propagation-selector');

    expect(within(selector()).getByRole('status')).toHaveTextContent(
      'Loading Knowledge resources',
    );
    expect(screen.queryByText('No Knowledge resources are available')).not.toBeInTheDocument();

    view.rerender(<Harness items={[]} />);
    expect(within(selector()).getByRole('status')).toHaveTextContent(
      'No Knowledge resources are available',
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    view.rerender(
      <Harness
        error="Knowledge inventory is temporarily unavailable."
        onRetry={retry}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Knowledge inventory is temporarily unavailable.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});
