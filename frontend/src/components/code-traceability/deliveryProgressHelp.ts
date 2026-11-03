export const deliveryProgressHelp = `
## Task progress — What changed and what remains

Open a task's **Recorded progress** to see meaningful work updates and remaining work. The agent records changes that matter: implementation changes, a blocker appearing or being resolved, and a handoff or closeout with new information.

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

An uncertain response requires checking the original operation before retrying. Changed task or Spec context requires refreshing the affected information and reviewing the draft. The system never treats a missing response as delivery.

**Details** and the task's history retain the technical evidence for inspection. Human exceptions remain separate decisions and do not create passing tests. Actions and visible records depend on your permissions.
`;
