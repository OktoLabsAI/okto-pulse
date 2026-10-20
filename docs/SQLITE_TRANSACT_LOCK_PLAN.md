# SQLite transaction lock and durable KG projection plan

Date: 2026-10-19. Initial target: `okto-pulse-core` and `okto-pulse` main, version 0.4.0. Development branch: `feature/v0.4.1` in both repositories; release target 0.4.1.

## Goal and invariants

An accepted Spec or Card write commits its source data and a durable projection intent in one short SQLite transaction. Graph preparation and commit run without a SQLite writer transaction. The worker acknowledges a projection in a separate short SQLite transaction only after the graph's durability barrier. Every crash boundary has a deterministic recovery path. An acknowledgement for revision N cannot discard revision N+1. Destructive operations and rebuilds cannot race an older graph writer into resurrecting deleted data. Every process that can write the graph respects the same fencing protocol.

SQLite WAL still allows one writer at a time. The objective is to remove the worker's long reservation, not to promise lock-free concurrent writes. If confirmed writes must survive OS/power failure, use `synchronous=FULL` for every SQLite writing connection, with measured latency impact.

## Protocol

1. In the source unit of work, persist the change, the monotonic desired projection revision, and its outbox/queue intent atomically. Record invalidations caused by dependencies, not only `Spec.version` changes.
2. Claim work in a short transaction. Load a consistent source snapshot and close the SQLite read transaction. Prepare the deterministic graph changes without a SQLite writer lock. Store a durable operation record with stable ID, input revision/manifest, graph incarnation, deletion generation, projector version, and sufficient recovery data.
3. Under the board graph guard, validate the operation against current generation and graph epoch. Apply changes and an operation receipt in the same native graph transaction. The receipt must survive restart and identify the completed operation and outputs needed for SQLite finalization. A post-durable error is treated as a committed but incompletely finalized operation.
4. In a new short SQLite transaction, record the applied revision, audit and derived records idempotently, and acknowledge only the specific operation/revision. If a newer revision exists, preserve its pending intent. Release the graph guard after reconciliation.
5. On startup and before conflicting board work, reconcile prepared operations against durable graph receipts. Retry absent operations, finalize present operations, and quarantine genuinely indeterminate outcomes until the graph can be inspected. Never infer success from an in-memory report alone.

## Coordination and rollout

Keep graph serialization per board. For delete, rebuild and restore, acquire the board graph guard before starting a SQLite write; use stable lock ordering for multi-board work. Strengthen stale-owner fencing at the actual graph commit. No HTTP path may wait for the graph while holding `BEGIN IMMEDIATE`. Introduce additive schema and worker protocol versioning so old and new workers cannot consume the same active queue. Finish in-flight operations before changing protocol or rolling back. Keep checkpoint maintenance independent of normal graph commits only after confirming native WAL recovery in the pinned Grafx version.

## Implementation sequence

1. Capture current behavior and identify all code paths, ports, receipts and destructive operations. Add a progress ledger.
2. Add persistent revision/operation schema, SQLite durability settings and adapters, plus core port contracts.
3. Add graph receipt persistence and recovery, then refactor worker stages and acknowledgement.
4. Refactor destructive operations and graph guard ordering. Add compatibility and recovery startup behavior.
5. Test lock duration, concurrent writes, crash boundaries, duplicate delivery, newer revision, delete/rebuild, stale owner, and restart. Measure SQLite writer hold time and projection convergence before considering the work complete.

## Acceptance criteria

During a deliberately stalled graph commit, an unrelated SQLite write succeeds without waiting on that graph operation. The accepted source write and its intent survive restart. A crash after graph commit and before SQLite acknowledgement is reconciled exactly once. A newer source revision remains queued when an older operation is acknowledged. Delete/rebuild prevents old graph operations from reappearing. No acknowledgement precedes a graph durability barrier. Graph and SQLite error paths retain enough durable state to recover.

## Execution decisions and status (2026-10-19)

The 0.4.1 implementation uses the existing source event/outbox and coalesced queue as the durable desired-projection intent. The queue's unique claim token identifies the attempted revision; the projected content hash identifies its source snapshot. A newer source event invalidates the claim and leaves the queue pending, so the older ACK cannot discard it. These existing fields replace new desired/applied revision columns. The existing `domain_events` table holds the append-only prepared/finalized/absent journal without changing the governed SQLite schema fingerprint.

The prepared journal records protocol 1, the producing Community version, claim lineage, source identity, graph commit-history baseline, and recovery output. A different or corrupt protocol fails closed and leaves the operation unsettled. The baseline includes the native graph database identity and sequence; the operation ID is committed as native Grafx metadata. No separate graph node is needed for the receipt.

Version 0.4.0 cannot be made to honor a new journal marker retroactively. The rollout fence is therefore operational: stop every 0.4.0 process that can write this database, wait for its graph scopes to exit, deploy paired 0.4.1 Core and Community distributions, then start recovery before queue processing. Before any later protocol change or rollback, settle all prepared operations with the currently compatible worker. Never run mixed 0.4.0/0.4.1 workers on one live database.

Local acceptance passed with an approximately 570 KB Spec distributed across fields smaller than Grafx's 65,536-character per-value limit. With the native graph commit deliberately paused, an unrelated SQLite write committed in 0.008 s in two runs; the full projection took 9.787-10.817 s. An injected failure after native commit and before SQLite ACK recovered in 1.573-2.792 s without duplicate audit. The paired-wheel release gate also passed using the local pinned Grafx 0.0.7 checkout and an isolated Python 3.11 environment. These are one Windows test fixture's measurements, not production throughput estimates. Physical power-cut testing and production latency monitoring remain operational qualification work.
