export const taskRequirementHelp = `
## Task requirements — Change what must be delivered

Open **Change task requirement** from a task's Delivery tab, or select tasks from the Spec's Delivery view. Choose the tasks, select one requirement and save. A note is optional. Only an authorized human can save this decision; agents can inspect the current rules and prepare a request.

| Choice | Effect |
| --- | --- |
| **Required** | The task's current contributions must meet the applicable delivery checks. Also use this to restore a previous requirement. |
| **Optional** | The task's contributions can be omitted from the required delivery. Any work and history remain visible. |
| **Waived** | A human has explicitly dispensed with the task's contributions for this Spec edition. The decision remains visible. |

### What changes when you save

The decision applies to the selected tasks' current contributions in the current Spec edition. It does not mark tasks Done, create implementation proof, pass tests or change dependencies. Other tasks sharing a requirement remain responsible for their own contributions.

For example, waive an integration task that is no longer needed: it remains visible as **Waived**, with its existing work status and history. It does not appear as a delivered task. Restore **Required** later and its delivery checks apply again; earlier work must still have current evidence.

A Test Card decision applies to that card's execution responsibility. Required scenarios and criteria for other tasks or the Spec still need the applicable verification. A task decision cannot remove a policy finding or an independent completion check.

### Scope, history and changed information

A new Spec edition or changed task contributions require a new decision. An old decision stays in history and cannot silently excuse a different scope. Finished, cancelled or submitted tasks must follow their normal reopen workflow before changing their requirement.

Saving multiple selected tasks is one operation: either all decisions are saved with the authenticated author and time, or none are saved. Restoring a requirement adds a new decision and preserves the earlier one. Notes can be left blank; the author, time, affected tasks and choice are always recorded.

If the task or Spec changed, **Review latest task requirements** refreshes the options and keeps your note and choice for review before saving again. If the response is uncertain, **Retry same decision** checks the original submission without creating a duplicate. This does not automatically apply your draft to changed work.

Visible actions depend on your permissions. **Details** retains the existing evidence, test and governed exception tools for investigation. These remain separate from changing a task's requirement.

Open **Details · Decision history** for the selected task to inspect the author, time, choice and note. History loads only when opened, in pages. **Older decisions** and the edition selector let you inspect previous decisions; **Read full note** opens a long note on demand. Historical decisions do not establish the current requirement shown above.
`;
