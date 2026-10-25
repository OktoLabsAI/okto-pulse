/** Operator comparison switch supplied by the same compatible server binary. */
export function readinessReadMode(): 'projection' | 'legacy' {
  if (typeof window === 'undefined') return 'projection';
  return (window as typeof window & { OKTO_PULSE_CONFIG?: { READINESS_READ_MODE?: string } })
    .OKTO_PULSE_CONFIG?.READINESS_READ_MODE === 'legacy' ? 'legacy' : 'projection';
}
