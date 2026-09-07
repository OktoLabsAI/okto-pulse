# Grafx buffer pool budget

Community accepts `KG_GRAFX_BUFFER_POOL_MB` in the environment or `.env`, or
`CommunitySettings(kg_grafx_buffer_pool_mb=128)`. The unit is MiB (1,048,576 bytes).
The default remains **64 MiB**, matching Grafx. Values must be positive integers.
Restart Pulse after changing this startup setting; it is not hot-reloaded.
This source milestone covers backend configuration and connector forwarding.
Menu > Settings controls and persisted-settings wiring are a separate milestone;
their worktree implementation is not included in this reader commit.

The budget is **per database handle**, not a process-wide memory limit. The routed
Community composition has one writer lane and two independent read lanes. Each
opened lane gets the same budget, including pools reused by Board and Global
providers. With all three lanes open for one database, the nominal page-buffer
envelope is:

| Per-handle setting | Three open lanes |
|---:|---:|
| 64 MiB (default) | 192 MiB |
| 128 MiB | 384 MiB |
| 256 MiB | 768 MiB |

Multiply again by the number of resident database paths. Temporary recovery or
restore handles opened through the pool use the same budget and add their own
capacity while open. The buffers are populated on demand; these figures describe
their configured capacity, not immediate allocation or total process RSS. Query
memory, vectors, Python objects and other engine state are outside this budget.

For a non-default value, Community forwards `buffer_budget_bytes` to the Grafx
connector. Existing custom connectors retain their historical call signature at
the 64 MiB default. A custom connector used with another budget must accept and
honor `buffer_budget_bytes`; unsupported options fail the open without caching a
handle or retrying with the budget discarded. A shared pool must have the same
budget as the composition's settings.

This configuration does not change persisted page size, snapshots, writer
participation, WAL, durability, or Grafx's engine default. It applies to the
managed pools; independently constructed logical-transfer candidates keep their
own `connect_options`. Select 128/256 MiB only from measurements and the memory
envelope of the intended deployment.
