/**
 * Pulse C4 / TS5 — Q&A answer submission is single-in-flight, retries exactly
 * once on a retryable conflict, and surfaces the real server message.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SpecQAItem } from '@/types';

const apiMock = vi.hoisted(() => ({
  listSpecQA: vi.fn(),
  createSpecQuestion: vi.fn(),
  createSpecChoiceQuestion: vi.fn(),
  answerSpecQuestion: vi.fn(),
  deleteSpecQuestion: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('react-hot-toast', () => ({ default: toastMock }));
vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
    />
  ),
}));

import { QATab } from '../SpecModal';

const choiceQuestion: SpecQAItem = {
  id: 'qa-1',
  spec_id: 'spec-1',
  question: 'Which storage engine?',
  question_type: 'choice',
  choices: [
    { id: 'opt_0', label: 'Kuzu' },
    { id: 'opt_1', label: 'SQLite' },
  ],
  allow_free_text: false,
  answer: null,
  selected: null,
  asked_by: 'agent-alpha',
  answered_by: null,
  created_at: '2026-09-01T00:00:00Z',
  answered_at: null,
};

const textQuestion: SpecQAItem = {
  ...choiceQuestion,
  id: 'qa-2',
  question: 'Why this scope?',
  question_type: 'text',
  choices: null,
};

function conflict(message = 'Q&A version conflict') {
  return new AuthenticatedFetchError({ message, status: 409 });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderQATab(items: SpecQAItem[] = [choiceQuestion]) {
  apiMock.listSpecQA.mockResolvedValue(items);
  return render(<QATab specId="spec-1" mentionables={[]} onChanged={vi.fn()} />);
}

async function openChoiceAnswerForm() {
  fireEvent.click(await screen.findByText('Answer this question'));
  fireEvent.click(screen.getByRole('button', { name: 'Kuzu' }));
  return screen.getByRole('button', { name: 'Submit' });
}

describe('SpecModal Q&A answer submission', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('disables the submit button while the answer is in flight and re-enables it after', async () => {
    const pending = deferred<SpecQAItem>();
    apiMock.answerSpecQuestion.mockReturnValue(pending.promise);
    renderQATab();

    const submit = await openChoiceAnswerForm();
    expect(submit).not.toBeDisabled();

    fireEvent.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());

    pending.reject(new AuthenticatedFetchError({ message: 'boom', status: 500 }));
    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('boom'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' })).not.toBeDisabled());
  });

  it('calls the API exactly once when the submit button is double clicked', async () => {
    const pending = deferred<SpecQAItem>();
    apiMock.answerSpecQuestion.mockReturnValue(pending.promise);
    renderQATab();

    const submit = await openChoiceAnswerForm();
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(1));

    pending.resolve({ ...choiceQuestion, answered_at: '2026-09-02T00:00:00Z' });
    await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Answer posted'));
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(1);
  });

  it('reloads the Q&A list and retries once on a retryable 409 conflict', async () => {
    apiMock.answerSpecQuestion
      .mockRejectedValueOnce(conflict())
      .mockResolvedValueOnce({ ...choiceQuestion, answered_at: '2026-09-02T00:00:00Z' });
    renderQATab();

    const submit = await openChoiceAnswerForm();
    await waitFor(() => expect(apiMock.listSpecQA).toHaveBeenCalledTimes(1));
    fireEvent.click(submit);

    await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Answer posted'));
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(2);
    // one refresh before the retry + the usual post-success refresh
    expect(apiMock.listSpecQA).toHaveBeenCalledTimes(3);
    expect(toastMock.error).not.toHaveBeenCalled();
  });

  it('surfaces the server message when the single retry also conflicts', async () => {
    apiMock.answerSpecQuestion
      .mockRejectedValueOnce(conflict())
      .mockRejectedValueOnce(conflict('still conflicting'));
    renderQATab();

    const submit = await openChoiceAnswerForm();
    fireEvent.click(submit);

    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('still conflicting'));
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(2);
    expect(toastMock.success).not.toHaveBeenCalled();
  });

  it('does not retry a non-retryable error and shows the server message', async () => {
    apiMock.answerSpecQuestion.mockRejectedValue(
      new AuthenticatedFetchError({ message: 'boom', status: 500 }),
    );
    renderQATab();

    const submit = await openChoiceAnswerForm();
    fireEvent.click(submit);

    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('boom'));
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(1);
  });

  it('keeps the text answer button single-in-flight too', async () => {
    const pending = deferred<SpecQAItem>();
    apiMock.answerSpecQuestion.mockReturnValue(pending.promise);
    renderQATab([textQuestion]);

    fireEvent.click(await screen.findByText('Answer this question'));
    fireEvent.change(screen.getByPlaceholderText('Type your answer... (@ to mention)'), {
      target: { value: 'because' },
    });
    const answerButton = screen.getByRole('button', { name: 'Answer' });

    fireEvent.click(answerButton);
    fireEvent.click(answerButton);
    await waitFor(() => expect(answerButton).toBeDisabled());
    expect(apiMock.answerSpecQuestion).toHaveBeenCalledTimes(1);

    pending.resolve({ ...textQuestion, answered_at: '2026-09-02T00:00:00Z' });
    await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith('Answer posted'));
  });
});
