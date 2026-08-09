# Token Usage — MCP context cost

Measured context cost of connecting an agent to Okto Pulse over MCP.


Estimated context cost for an agent connected to the Pulse MCP server
(measured with tiktoken `cl100k_base` against the live surface).

### Fixed cost per connection

| Component | Tokens |
| --- | --- |
| Server `instructions` (agent operating instructions) | ~2.0K |
| `tools/list` — 313 tools (name + description + JSON schema) | ~48.2K |
| **Total at connect** | **~36.5K** |

With prompt caching this block is paid in full only on the first turn of a
session. Clients that load tool schemas lazily (e.g. Claude Code's deferred
tools) skip most of the `tools/list` cost upfront.

### On-demand resources

Agents fetch `okto-pulse://` resources per the mandatory protocol — only what
the current flow needs:

| Typical flow | Resources read | Tokens |
| --- | --- | --- |
| Session start (mandatory preflight) | `workflows/preflight` | ~1.1K |
| Working a card | preflight + cards + transitions + card_types | ~7.7K |
| Authoring a spec | preflight + specs + spec_gates | ~4.6K |
| Operating the KG | preflight + kg + kg-health | ~7.5K |

(Full corpus, which the protocol never requires reading at once: workflows
~20K + reference ~20K + tool-docs ~40K ≈ 80K.)

### Variable cost: tool responses

Response payloads dominate real sessions. Typical calls cost hundreds of
tokens to a few K; the outliers matter: `list_by_board(entity_type=spec)`
returns full entity bodies (tens of K on large boards — prefer a low
`limit`), `get_*_context(profile="full")` on a large spec reaches several K,
and `get_refinement` embeds the full parent-ideation context.

### Session profiles (ballpark)

| Session | Estimate |
| --- | --- |
| Short triage (few reads) | ~45–60K tokens |
| Full card execution (pre-flights + validation) | ~60–100K |
| Heavy SDLC session (spec authoring + saturation) | 100–200K+ |

The dominant remaining lever is lazy tool loading by role (would cut most of
the ~34.5K `tools/list` cost per session) and summary-first projections on
large listing payloads.

---

[← Back to README](../README.md)

