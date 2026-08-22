interface SpecEditionLabelProps {
  edition: number;
  technicalRevision: number;
  className?: string;
}

/** Human-facing Spec edition. Technical revision belongs in audit views only. */
export function SpecEditionLabel({
  edition,
  technicalRevision,
  className,
}: SpecEditionLabelProps) {
  void technicalRevision;
  const description = `Edition ${edition}`;
  return (
    <span className={className} title={description} aria-label={description}>
      Edition {edition}
    </span>
  );
}
