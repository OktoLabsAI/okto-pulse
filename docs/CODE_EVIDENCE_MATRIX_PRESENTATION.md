# Code Evidence Matrix: associations and coverage

The matrix displays every server-projected `obligation_evidence_mappings` association
for the inherited evidence rows, in its corresponding Spec/FR/TR/BR/AC/API/IR/OR/Decision/Test
column. Obligation titles are preferred; missing titles fall back to the type and ID.
The hover description includes the full reference and relation type.

- Applicable evidence retains its existing presentation.
- `evidence_applicable=false` associations are labeled **Contextual link**. These
  references are visible but are not evidence of delivered implementation.
- Unresolved applicability is labeled **Applicability unresolved**. Displaying the
  association does not resolve classification or establish coverage.
- A dash means no projected association for that evidence/column, not merely a
  non-applicable association.

Coverage totals, percentages, disposition and gate decisions remain server-owned.
This UI correction neither changes the Skip setting nor introduces an implementation
evidence requirement. Inherited-snapshot membership remains unchanged.

Regression coverage: `CodeTraceabilityPanels.test.tsx`, `sourceContextPresentation.test.ts`
and the isolated Chromium scenario `tests/canvas/evidence-matrix.spec.ts`.
