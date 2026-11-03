import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { HelpPanel } from '../HelpPanel';

afterEach(cleanup);

it('makes task requirement rules explicit in central Help, using the contextual destination', () => {
  render(<HelpPanel initialSectionId="task-requirements" onClose={vi.fn()} />);
  const content = screen.getByRole('heading', { name: 'Task requirements — Change what must be delivered' }).parentElement!;
  for (const rule of ['Required', 'Optional', 'Waived', 'A note is optional', 'Only an authorized human',
    'It does not mark tasks Done', 'Other tasks sharing a requirement', 'Required scenarios',
    'A new Spec edition', 'Restoring a requirement adds a new decision',
    'either all decisions are saved', 'Review latest task requirements', 'Retry same decision']) {
    expect(content).toHaveTextContent(rule);
  }
  expect(content).not.toHaveTextContent('task-requirement-decision/v1');
});
