import type { ChoiceOption } from '@/types';

/** Native Q&A metadata is informative; selection belongs to the answer control. */
export function ChoiceOptionContent({ option }: { option: ChoiceOption }) {
  return <span className="min-w-0 break-words">
    <span>{option.label}</span>{' '}
    {option.recommended === true && <span className="ml-2 inline-block rounded bg-blue-100 px-1.5 py-0.5 text-xs font-medium text-blue-800 dark:bg-blue-900/40 dark:text-blue-200">Recommended</span>}
    {option.tradeoff?.trim() && <>{' '}<span className="mt-1 block text-xs font-normal text-gray-600 dark:text-gray-300">{option.tradeoff}</span></>}
  </span>;
}
