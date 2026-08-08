export interface PulseErrorMessageProps {
  summary: string;
  details: string;
  copyText: string;
}

/**
 * Marker rendered as the toast message. PulseToastCard recognizes its props
 * and exposes structured metadata without leaking stacks or sensitive fields.
 */
export function PulseErrorMessage({
  summary,
}: PulseErrorMessageProps) {
  return <>{summary}</>;
}
