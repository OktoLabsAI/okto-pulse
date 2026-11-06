/** Shared English Help for the Coverage screen and central application guide. */
export const coverageHelp = `
# Spec coverage

Coverage separates planning from delivery. **Planning coverage** measures links
between requirements, rules, scenarios and tasks. A linked task or scenario does
not prove that work was delivered or a check passed.

**Delivery status** shows the application's recorded task groups: Delivered,
In progress, Pending and Optional / Waived. **Implementation** and **Verification**
are independent percentages from the persisted delivery assessment. Coverage is
informational; it does not approve the Spec or authorize completion. Open
**Validation** for the Spec validation result.

The requirement matrix identifies each requirement, its implementation and
verification, the impact of missing or failed checks, and the next action.
An unknown assessment is not zero or success. **Not applicable** means there is
no applicable work in that phase. Optional and waived work does not count as proof.

## Freshness and availability

Planning, delivery status, delivery assessment and graph observations have
independent freshness. **Updating** means a new calculation or read is pending.
**Previous data** remains visible only within the same authorized scope and is
not a current approval. **Unavailable** means the current facts could not be
confirmed. Refresh the Spec to check again.

Graph details may be temporarily unavailable while planning and delivery remain
readable. Missing graph observations do not mean missing implementation. A changed
source needs a new observation; a permission failure removes the affected data.
Changing the Board, Spec, edition or access scope clears old observations.

## Details and refresh

Use **Show details** for the existing planning and implementation views, graph
observations and technical evidence. Opening it requests graph details once.
**Hide details** makes no request. The **Refresh** button in the Spec modal
refreshes the active Coverage mode and delivery reads. The Coverage summary does
not poll or automatically retry graph requests. Refresh restarts pagination;
pages from different modes, sources or assessment generations are never combined.

## Human updates and receipts

People describe their work in a task's Delivery screen. The application creates
the submission receipt automatically. People do not enter receipt IDs or arrange
technical proof. A human submission records a declaration; the responsible agent
still supplies implementation evidence and verified results. Authorized humans
can use **Change task requirements** to choose Required, Optional or Waived,
with an optional note. That decision does not create delivery proof.
`;
