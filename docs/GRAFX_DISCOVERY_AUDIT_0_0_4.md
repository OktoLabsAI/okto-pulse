# Discovery: native Grafx execution and semantic audit

Date: 2026-09-07. Pulse 0.3.3, local Grafx 0.0.4 development sources.
Board: `15877207-c147-4805-96d7-d53a625571df` (Okto Pulse).

## Architecture and delivered corrections

- Grafx commit `6b0b66e` implements a generic labelled anchor followed by one correlated
  optional incident hop. It supports directions, typed/untyped relationships, optional WHERE,
  null extension, aggregation and existing row windows. Missing polymorphic properties are
  null; incompatible families fail before streaming. Multiwriter/multireader, snapshots, owner
  overlays, query budgets, WAL and durability remain unchanged.
- Key Decisions sends the original Core Cypher through the Community executor. No special
  query recognizer, degree counter or ranking algorithm was added to the adapter. Core remains
  backend-neutral; the relevance/connectivity ranking is a Pulse product rule.
- FR coverage includes explicitly linked scenarios, business rules and cards. Numeric AC
  positions are not inferred to be FR positions. Card provenance is retained.
- Spec decision chains are scoped by `(spec_id, decision_id)`, independent of JSON ordering,
  and bounded for cycles/missing targets. Incomplete chains produce diagnostics, not hangs.
- Nullable contradiction confidence displays `n/a`, not HTTP 500 or invented confidence.
- An absent Learning area omits the Community query filter; `CONTAINS NULL` retains its correct
  Cypher semantics in Grafx. A nonempty area still filters normally.
- Natural-query warnings survive normalization. UI warnings are visible, including partial
  results; failed empty results are no longer described as successful empty reads.
- Uncovered coverage explicitly includes archived card facts when deciding whether a link
  still covers a requirement. The neutral read port defaults to excluding archived cards for
  other callers; Community's SQL adapter implements the opt-in with board scoping intact.

## Evidence

- Grafx grouped regression: 421 passing tests across parser, planner, OPTIONAL MATCH,
  polymorphic nodes, untyped traversal and ordered merge. Follow-up native feature slice:
  13 passing tests.
- Community real-Grafx integration/store/executor group: 61 passed. Additional native
  Key Decisions/Learning ranking group: 3 passed.
- UI warning/result-state tests: 13 passed; production frontend build completed.
- Relational-card semantic tests use the neutral read port and exact IDs, metadata and scope
  assertions: final combined Core slice passed 53 tests. The Community archived-reader SQL
  integration passed 1 test. Neither substitutes for the live-board checks below.
- Live Key Decisions API and actual UI click returned 100 ranked decisions without warning.
  Top observed node had 8 incident connections. API observations were approximately 1.6–3.1 s;
  these are observations, not a new performance gate or controlled engine benchmark.
- Graph inventory: 2,229 canonical nodes, including 142 Decisions, 0 Learnings and 3 Bugs.
  Edge diagnostics read all 70 physical tables successfully; no `contradicts` edges were present.

## Final live card matrix

All 14 cards returned HTTP 200 without warning after restart (PID 6876). All 14 were also
clicked/submitted through the actual frontend; rendered row counts matched API payloads.
The FR/card selectors were exercised, not bypassed through DOM mutation.

| Card | Observed rows after final refresh | Meaning checked |
| --- | ---: | --- |
| Recent activity | 50 | Recent board activity, navigation metadata |
| My mentions | 0 | Current user's mention token, board scoped |
| FR coverage | 2 | FR `fr_c601dcd0`: one explicit rule and one linked card, both rendered |
| Uncovered requirements | 9 | Canonical coverage calculation; archived-card correction included |
| Scenarios without tasks | 10 | Matches source scenarios without linked task IDs |
| Decisions by topic (`graph`) | 1 | Decision type and similarity threshold |
| Key Decisions | 100 | Native degrees, relevance/degree ranking, top-100 result window |
| Contradictions | 0 | No contradictory edges in current board; positive native store test |
| Superseded decisions | 0 | Spec-local structured decision chains; no source supersedes links |
| Current sprint blockers | 0 | No active sprint on this board; positive blocker fixtures |
| Card dependents | 10 | Exact reverse dependency count for card `7fcf0845-d465-509b-9839-1cd2013120a6` |
| Similar nodes (`graph`) | 20 | Native results with IDs/types/similarity; warnings preserved |
| Learnings by relevance | 0 | No Learning nodes; positive native ordering fixture |
| Learnings from bugs | 0 | No Learning nodes; positive native Learning→Bug fixture with absent/present area |

## Existing scope limits, not silently expanded

Key Decisions ranks at most 500 query candidates and returns 100. Thus its current contract
does not guarantee a global top-100 when a board has more than 500 Decisions. Learnings by
relevance returns at most 200. These are existing Pulse result-window limitations, not a new
Grafx parser failure; the current live board is below the Decision candidate cap.

Superseded decisions reads spec-local structured source history, not all node-type graph
supersedence edges. Contradictions uses the schema's Decision→Decision contradiction relation.
Card dependents returns cards, not an invented spec-dependency relation. These distinctions
must remain explicit when interpreting an empty result or the broad wording of a catalog card.

No live source items, graph entities or audit records were synthesized for this audit.
Positive missing-data cases run only in isolated test fixtures.
