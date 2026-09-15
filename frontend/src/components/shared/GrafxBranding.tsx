import grafxIcon from '@/assets/okto-grafx-icon.svg';

/** Community graph-engine attribution; no external image request or runtime coupling. */
export function GrafxBranding({ className = '' }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300 ${className}`}>
      <img src={grafxIcon} alt="" aria-hidden="true" width={28} height={28} className="h-7 w-7 shrink-0 object-contain" />
      <span className="whitespace-nowrap">Powered by Okto Grafx</span>
    </span>
  );
}
