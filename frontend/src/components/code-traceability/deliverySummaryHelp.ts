/** One user-facing guide shared by central Help and the contextual destination. */
export const deliverySummaryHelp = `
## Delivery status — See what has been delivered

Open a Spec's **Coverage → Implementation** view to see its delivery status.
The main screen answers three questions: what was delivered, what is in progress and what is pending.

### A task's Delivery tab

The main view shows, in order: the current delivery status, **Implementation** and **Verification** percentages, the **Requirement / Objective** matrix when applicable, and the **Show details** / **Hide details** and **Change task requirements** buttons. Details starts closed. A check icon accompanies **Delivered**; an X accompanies **Verification failed**. The text conveys the same result without relying on color or the icon.

Each matrix row is an item assigned to this task, with independent implementation and verification results. The percentages come from the current assessment of this task, not from its workflow status or from the visible page alone. **100%** proof does not complete the task or replace the delivery verdict. **Unknown** and **Not applicable** are not zero. **Waived** and **Not applicable** do not create passing proof. Test Cards show their assigned test scenarios; implementation can be **Not applicable**. A task without applicable assigned items has no matrix. Use **Next items** or **First items** when the matrix spans more than one page.

Use **Change task requirements** to choose Required, Optional or Waived for this task; a note is optional. The button is next to Details and does not require opening the technical information. Users without edit authority see **View task requirements**. Progress forms, latest updates, history, receipts and technical diagnosis are inside Details.

| Task status | Meaning |
|-------------|---------|
| **Checking delivery / Delivery unavailable** | The current result is not confirmed. Wait for preparation or explicitly retry a failed read. |
| **Not required** | An authorized decision made the task optional or waived for this edition. It does not mean delivered. |
| **Verification failed** | Current verification contains a failure that needs attention. |
| **Previously completed** | Historical Done is preserved, but current proof no longer confirms delivery. |
| **Work remaining** | Implementation remains pending. The reason distinguishes not started, paused and cancelled work. |
| **Awaiting verification** | Implementation is present; required verification remains pending. |
| **Awaiting completion** | Evidence alone is insufficient: a report, review or other completion check remains. |
| **Delivered** | Current required proof and completion are confirmed. |
| **Ready to complete** | The current checks permit the next completion action; it has not yet been performed. |

A human update has its audit receipt generated automatically. This proves who submitted the text and when; it does not prove code execution, passing tests or approval. See **Task progress** for submission, retry and correction rules. There is no manual receipt form, including in Details.

**Show details** opens **Pending work and verification** directly, followed by progress and delivery forms, evidence history and test navigation. There is no second button to reveal the checks.

The pending-work table names the **Requirement / Objective**, its **Affected stage**, **What needs attention** and the **Next step**. A test scenario can be waiting for another task's implementation; its row names the related requirements and explains that they are prerequisites for verification. General completion checks appear separately under **Task-wide and unassigned checks**. When a requirement cannot be identified, the table says so instead of assigning the problem to an unrelated item. A check can affect several requirements, and one requirement can have several independent checks.

Open **Evidence and technical details** on an occurrence when you need its original assessment, references or full action plan. These details are optional; the responsible agent handles technical evidence and receipts. The table does not ask a human to supply a receipt. An assessment with no listed checks does not itself approve delivery; the status above remains authoritative.

Pages contain at most 20 checks; use **Next checks** or **First checks** when needed. Partial lists of impacted items are explicitly identified. Opening Details loads one page; closing and reopening the same current assessment reuses that page. Full technical evidence loads only when requested. Use **Refresh card** in the task modal header to update the status, percentages, matrix and open checks together; paginated views return to their first page. There is no separate refresh control in the Delivery overview. Pending generation checks are grouped and stop when the tab is inactive. After a network error, use the same header control, **Retry delivery assessment**, or **Retry delivery checks** as indicated; failed checks do not repeatedly reload in the background. Refreshing preserves an open draft and whether Details is expanded; submission is disabled until the assessment is current again.

Use Tab to reach **Show details** / **Hide details**, **Refresh card** or **How this works**, then Enter or Space to activate them. The help link opens this exact topic. Each form has named fields and errors; success and failure include visible text.

### Read the groups

| Group | Meaning | Next step |
|-------|---------|-----------|
| **Delivered** | A required task is Done and its current delivery checks and proof are satisfied. | Open **Details** if you need to inspect the evidence. |
| **In progress** | Work on the task is currently in progress. | Follow its progress or open the task to continue the work. |
| **Pending** | The task still needs work, validation, a dependency or confirmation of its current proof. | Read the short reason beside the task and address that step. |
| **Optional / Waived** | A human decision makes this task unnecessary for delivery in this Spec edition. | Expand the section to review it or change its requirement. It does not count as delivered work. |

**Awaiting validation** stays Pending. Finishing the work and approving its delivery are separate steps.
A task in **Done** can also appear as **Completion awaiting confirmation** when its proof is missing or out of date.
Its historical Done status is preserved; the delivery screen does not reopen it automatically.

**Waiting for a dependency** means another task must be resolved first. Open the task to inspect that dependency.
On-hold, rejected and cancelled tasks retain a short reason in Pending while they are required.

### Examples

- A task with complete implementation proof that is still awaiting validation remains **Pending**.
- A Done task whose proof is current appears in **Delivered**. If that proof becomes stale, confirmation is pending again.
- A waived task appears in **Optional / Waived**, even if its work status says Done or In progress.
- Two tasks can share one requirement. The main counts are **task counts**; one task's completion does not automatically deliver the other.

### Change what is required

Use **Change requirement** beside a task, or **Change task requirements** to select up to 50 tasks.
Use **Load more** to find further tasks, then continue with the selection.
Choose **Required**, **Optional** or **Waived**, add a **note if useful**, and save once.
Only an authorized human can make this decision. Read-only users can inspect the requirement and its history.

Saving changes the selected tasks' requirement for the **current Spec edition**. It does not mark tasks Done,
create proof, remove other tasks' responsibilities or satisfy the Spec's required test scenarios.
The whole selection is saved together. Restoring **Required** adds a new recorded decision;
it does not erase history or reactivate revoked proof. The **Task requirements** Help topic explains these choices in full.

If a conflict occurs, review the latest requirements. Your note and choice are preserved.
If the save response is uncertain, use **Retry same decision** before making another change.
An accepted save confirms the decision; it does not approve delivery.

### Updating, unavailable and empty

While status is **updating** or **being prepared**, completion and counts may not yet be confirmed.
Unknown counts are not zero. The screen makes a few automatic refresh attempts; use **Refresh delivery status** to check again.
An access error or unavailable calculation is not an empty Spec. Review your access with the Board owner, or refresh after the service recovers.
**No tasks in this Spec** appears only when the complete current task list is known to be empty.

### Details and traceability

**Details** opens the existing implementation, verification, coverage and evidence views.
Additional task pages and detailed evidence load when requested. Reopening Details preserves its local draft.
The simplified screen keeps the same history, permissions and delivery rules. Viewing it does not approve a Spec or a task.
Progress is reported when work changes meaningfully; the **Task progress** Help topic explains that policy.
Agent instructions complement these guides. The application Help and the link beside this screen remain the user-facing rules.
`;
