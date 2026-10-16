# Health observations

For a local runtime observation:

```bash
okto-pulse status
okto-pulse status --json
```

Status reports the configured data location, SQLite availability and counts,
serve-lock identity, and API/MCP port reachability. It opens an existing SQLite
database read-only; it does not open the graph, initialize the schema, enqueue
work, or repair data. Missing or uninitialized databases have an explicit state;
exit 0 alone does not establish readiness. A reachable port alone does not prove
that its listener owns this installation. SQLite errors return exit 1.

This observation does **not** certify graph integrity, projection currency,
worker progress, or availability of a particular Board's operations. The graph
runtime is Okto Grafx. Old instructions referring to Kùzu files, manual queue
updates, lock deletion or direct worker invocation are not supported procedures.

The former `verify-pipeline` command is removed: its execution initialized the
relational schema and composed graph access, so it could not provide a passive
health observation. `reset` and its physical deletion implementation are also
removed. The entire `kg` maintenance command group is also removed. These names,
including old options and help invocations, produce the
standard unknown-command error (exit 2) before dispatch. There is no replacement
alias or callable handler for either command.

An unavailable component must be reported with its reason and affected operation.
Health is not permission to repair storage, bypass a gate, or manufacture missing
evidence. Corruption requiring human intervention belongs to an authorized
external support/release procedure with a verified backup and compatible binaries.

The v0.4.0 maintenance removal is incremental on the feature branch. The dedicated recovery executor and existing UI/MCP/REST maintenance surfaces still require removal and
qualification; this CLI change does not establish that every health provider is
free of lazy initialization or writes. The consolidated implementation ledger
tracks those remaining requirements and their evidence.

See [CLI status contracts](CLI_ISSUE_CLOSEOUT_20260913.md#status-human-and-machine-readable-observations)
and the [current command list](../README.md).
