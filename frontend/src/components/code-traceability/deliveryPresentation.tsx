import { ChevronDown } from 'lucide-react';
import type { ReactNode } from 'react';

export const deliveryField = 'mt-1 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100';
export const deliveryButton = 'btn btn-secondary text-xs disabled:opacity-50';
export const deliverySection = 'space-y-3 rounded-lg border border-gray-200 p-3 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-400';
export const deliveryNotice = 'rounded-lg bg-gray-50 p-3 text-xs leading-relaxed text-gray-500 dark:bg-gray-800/50 dark:text-gray-400';
export const deliveryError = 'rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/20 dark:text-red-300';

/** Same compact header, badge and expanded typography as Functional requirements. */
export function DeliveryDisclosure({ title, badge, children }: { title: string; badge?: ReactNode; children: ReactNode }) {
  return <details className="group/delivery overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
    <summary className="flex cursor-pointer list-none items-center gap-2 bg-gray-50 px-3 py-2 dark:bg-gray-700/50">
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-900 dark:text-white" title={title}>{title}</span>
      {badge != null && <span className="shrink-0 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">{badge}</span>}
      <ChevronDown size={14} aria-hidden="true" className="shrink-0 text-gray-400 transition-transform group-open/delivery:rotate-180" />
    </summary>
    <div className="space-y-3 px-3 py-3 text-xs text-gray-600 dark:text-gray-400">{children}</div>
  </details>;
}
