# Operator authorization for long external test runs

External unittest profiles keep their default 60-second timeout and 120-second
ceiling. A local operator can explicitly authorize a larger timeout, capped at
3600 seconds, when registering a new scoped profile:

```text
okto-pulse test-runner campaign-v2 --workspace ABSOLUTE_WORKSPACE --source-ref SOURCE --board-id BOARD --spec-id SPEC --scenario-id SCENARIO --test tests/campaign.py::Campaign.test_run --timeout 900 --allow-long-running
```

Registration does not overwrite existing profiles. The new optional profile
field `timeout_authorization: local-long-running/v1` records local authorization;
the timeout and authorization are validated again on every invocation. Existing
profiles are unchanged. Older installations reject profiles containing this new
field rather than silently ignoring their authority.

The MCP request still accepts only `runner_ref` and an immutable `revision`.
Remote callers cannot increase a timeout, add authorization, change selectors,
or supply commands. This is local host execution authority, not a remote token.

The timeout applies to the whole selected unittest process, not each test.
Snapshot construction precedes that timer. Existing process-tree termination
and `timeout_no_receipt` behavior remain in place; a receipt is issued only
after the actual process completes and its result passes validation. Skipped,
incomplete and aborted runs do not become successful evidence. No detached
background continuation or replay of precomputed results is introduced.

Long runs occupy an execution thread and an MCP request for their duration.
Client/proxy request limits may be shorter than the authorized runner timeout;
operators must verify that transport separately. This extension does not change
the existing asynchronous job API or guarantee request delivery or receipt aggregation.
Increasing the runner limit does not relax a product's performance criteria.

Deployment, service restart and new profile registration are separate operator
actions. The proposal and its tests do not install themselves or modify an
already running installation.
