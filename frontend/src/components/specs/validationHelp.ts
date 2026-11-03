/** Shared wording for the result card and the application's validation guide. */
export const validationVerdicts = {
  validated: ['Spec validated', 'All required planning checks passed for this edition.'],
  validated_with_exception: ['Spec validated with exceptions', 'Validation is complete with recorded human exceptions.'],
  ready_to_conclude: ['Ready to conclude validation', 'The required checks are satisfied. Validation still needs to be concluded.'],
  not_validated: ['Spec not yet validated', 'Required planning checks or the validation conclusion are still pending.'],
  unavailable: ['Validation could not be confirmed', 'Refresh or check your access to confirm this edition’s validation.'],
  not_required: ['Validation not required', 'Validation is disabled and the other required planning checks are satisfied.'],
} as const;

export const specValidationHelp = `
## Spec validation — Understand the result

Open a Spec's **Validation** tab to see the overall result for its current edition. This checks whether the implementation plan has been validated. Delivery and execution tests have their own results.

### What each result means

| Result | Meaning |
|--------|---------|
${Object.values(validationVerdicts).map(([title, description]) => `| **${title}** | ${description} |`).join('\n')}

### Your next step

- **Pending:** use **Review pending check** to open the relevant section. For example, quality can pass while task decomposition still needs attention; the overall result remains **Spec not yet validated**.
- **Ready:** an authorized reviewer can use **Conclude planning validation**. Opening the screen or refreshing it never approves the Spec.
- **Unavailable:** refresh or ask for access. A missing result, a failed read or limited access cannot confirm approval.
- **Validated with exceptions:** open **Details** to review the effective human decisions and their scope. A dispensed requirement does not become a passing technical test.

### Details and history

**Details** starts closed. Open it for Spec Validation, Task decomposition, Checklist, Requirement lint and Policy Compliance, according to your permissions. The individual systems keep their own results and history. Collapsing Details preserves drafts while you remain in the same Spec edition.

The main result always concerns the **current edition**. A new edition needs its own validation; earlier approvals remain in history. If current checks need attention after an earlier conclusion, the main result shows the current situation and Details retains the recorded conclusion.

Draft and Review show that validation has not yet been concluded. Disabled quality validation only produces **Validation not required** after the other required checks are satisfied. Advisory findings remain available in Details.

### Examples

- All required checks pass and the reviewer concludes validation: **Spec validated**.
- Quality passes but a required decomposition review fails: **Spec not yet validated**.
- The current plan is concluded with an effective human exception: **Spec validated with exceptions**.
- The previous edition passed but the current edition has no accepted result: **Spec not yet validated**.

Actions depend on your permissions and the Spec's lifecycle. Every change is checked again when submitted. Use **How validation works** beside the result to return directly to this guide.
`;
