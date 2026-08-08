interface SpecEditionLabelProps {
  edition: number;
  technicalRevision: number;
  className?: string;
}

/** Human-facing Spec edition with its internal revision available on demand. */
export function SpecEditionLabel({
  edition,
  technicalRevision,
  className,
}: SpecEditionLabelProps) {
  const description = `Edition v${edition}; technical revision r${technicalRevision}`;
  return (
    <span className={className} title={description} aria-label={description}>
      v{edition}
    </span>
  );
}
