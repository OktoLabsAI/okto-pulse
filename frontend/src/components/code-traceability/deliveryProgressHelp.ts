export const deliveryProgressHelp = `
## Task progress — What changed and what remains

Open a task's **Delivery** tab to see its current status, latest update and remaining work. Choose **Record an update**, describe what changed and optionally what remains, then **Save update**. The application creates the audit receipt automatically. You never need to obtain, select or enter a receipt, technical target, impact classification or assessment score.

### Submit a delivery

While the task is In Progress, choose its delivery action or move it to Validation / Done. The same simple form opens. The application reuses eligible technical evidence and assessments already recorded by the responsible agent. If any required proof, assessment, permission or review is missing, the submission is refused and the reason identifies the next step. Your text stays in the form. It is not silently saved as progress and the task is not marked delivered.

This also applies to **Test Cards**: describing a test does not create a passing run. A test result must come from its governed verification workflow. A task already awaiting validation needs the validator's decision; writing another report does not bypass that review.

### Retry or correct an update

If a response is lost, choose **Retry same submission**. The original content and submission identity are retained in memory so the application can confirm the original save without duplicating it. Wait for confirmation before closing the form. Drafts are not stored in browser local storage.

After a version conflict, use **Refresh task**, review the current task and your preserved draft, then **I reviewed the current task** before sending a new intent. A successful save confirms only that submission, not that the delivery passed verification.

To correct your update, open **Details → Delivery record history → Browse delivery records**, read the record and choose **Correct this update**. The application creates a new linked record. The original author, time and content remain unchanged. Corrections require current access and an In Progress task; another person's update cannot be rewritten.

### Fewer updates, the same history

An agent does not need to post an update every few minutes, after every tool or for every commit. A task's status change already records that event. Accepted progress, evidence and reports are kept in the task's history; repeating them in a comment adds no delivery proof.

### Reading an update

- **Work done:** what changed since the previous meaningful update.
- **Remaining:** what still needs implementation, verification or a decision.
- **Blocker:** the concrete obstacle and the next step, when known.

For example: “The validation summary is implemented. Remaining: screen integration and tests.” This describes progress while the task is still in progress.

### Progress and delivery

A progress update does not complete a task. Delivery requires the applicable implementation evidence, tests and review. A successful handoff to Validation means the work was submitted for review; the reviewer and completion checks still decide the outcome.

The agent can submit related evidence and the final report together. Each item still retains its author, scope and history. A failed submission saves none of that combined operation. Retrying the same accepted submission does not create another delivery or move the task again.

### Resuming work

Saved progress explains where work stopped. The agent must check access to the actual source and the current task before claiming the work was recovered. New changes can make earlier proof outdated; the affected implementation or tests need current evidence again.

For agents, an uncertain response requires checking the original operation before retrying. Changed task or Spec context requires refreshing the affected information and reviewing the draft. The system never treats a missing response as delivery.

**Details** and the task's history retain the technical evidence for inspection. Human exceptions remain separate decisions and do not create passing tests. Actions and visible records depend on your permissions.
`;
