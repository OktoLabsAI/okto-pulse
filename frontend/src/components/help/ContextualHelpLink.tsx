import type { ReactNode } from 'react';
import { HelpCircle } from 'lucide-react';

import {
  openContextualHelp,
  type HelpSectionId,
} from './contextualHelp';

export interface ContextualHelpLinkProps {
  sectionId: HelpSectionId;
  children?: ReactNode;
  ariaLabel?: string;
  className?: string;
  testId?: string;
}

export function ContextualHelpLink({
  sectionId,
  children = 'How this works',
  ariaLabel,
  className = '',
  testId,
}: ContextualHelpLinkProps) {
  return (
    <button
      type="button"
      onClick={() => openContextualHelp(sectionId)}
      aria-label={ariaLabel}
      data-testid={testId}
      className={`inline-flex items-center gap-1 font-medium text-violet-600 hover:text-violet-700 hover:underline focus:outline-none focus:ring-2 focus:ring-violet-500 focus:ring-offset-2 dark:text-violet-300 dark:hover:text-violet-200 ${className}`.trim()}
    >
      <HelpCircle size={12} aria-hidden="true" />
      {children}
    </button>
  );
}
