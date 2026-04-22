# MCP Audit Fix Plan — DS-MOZ Intelligence Gateway

**Source**: `docs/mcp-audit-2026-04-22.md`
**Target file**: `src/gateway/routes.py` (mostly)
**Projected score**: 35 → 56–58/60 (≈94%) — lifts verdict from 🟠 *Needs Refactoring* to ✅ *Agent-Ready*.

The plan is sequenced so each step is a self-contained, independently-mergeable commit. Steps 1–3 are purely additive and safe. Step 4 is a renaming with a short deprecation shim. Step 5 is schema work.

---

## Step 1 — Server-level instructions (Criterion 5, +3)

**File**: `src/gateway/routes.py:147`

Change:
```python
server = Server("DS-MOZ Intelligence Gateway")
```
to:
```python
server = Server(
    "DS-MOZ Intelligence Gateway",
    instructions=GATEWAY_INSTRUCTIONS,
)
```

Define `GATEWAY_INSTRUCTIONS` at module top — a ~15-line prompt covering:
- What the gateway is (multi-MCP proxy behind a single endpoint).
- The lifecycle: `browse_mcps` → `add_mcp` → `search_tools`/`list_mcp_tools` → `invoke_mcp_tool`.
- Meta-tools are free; `invoke_mcp_tool` deducts credits per call.
- The toolbox persists across sessions — no need to re-add each time.
- On credit exhaustion: direct user to the portal (`/portal/credits`).

**Acceptance**: `mcp_server.create_initialization_options()` serialises the instruction string; manual test via `curl` on `/gateway/{uid}` shows the instructions in the `initialize` response.

---

## Step 2 — Rewrite descriptions + parameter docs (Criterion 4, +4)

**File**: `src/gateway/routes.py:149-211`

For each of the 7 tools, replace the one-liner with a structured description:

```
<purpose in one line>

When to use: <sibling contrast — e.g. "vs browse_mcps which shows all...">
Returns: <shape hint>
Credit cost: <free | per-call via upstream>
Example: <one concrete call + expected summary of result>
```

Add `description` to every property in `inputSchema`. Concrete rewrites:

- **list_mcps** — "List MCPs already enabled in the caller's toolbox. Use `browse_mcps` to see what else is available."
- **browse_mcps** — "Browse the full catalogue of MCPs, including those not yet enabled. Each entry shows `enabled` and `credit_cost_per_call`."
- **add_mcp / remove_mcp** — add `mcp_slug` description: "Slug from `browse_mcps` (e.g. `dsmoz-intel`). Case-sensitive."
- **search_tools** — "Keyword search across tools of all *enabled* MCPs. Cheap and deferred-friendly — use this before `list_mcp_tools` when you don't know which MCP owns a capability." Add `query` description.
- **list_mcp_tools** (renamed) — "List tools exported by one specific enabled MCP. Use when you already know which MCP you need."
- **invoke_mcp_tool** (renamed) — "Proxy a call to a tool on an upstream MCP. CREDIT-GATED: each call costs `credit_cost_per_call` credits (see `browse_mcps`). Use `list_mcp_tools` first to discover valid `tool_name` and the shape of `arguments`. Errors return `{error: ...}` as JSON text."

**Acceptance**: descriptions ≥200 chars each; every parameter has `description`. No behaviour change.

---

## Step 3 — Surface `credit_cost_per_call` (Criterion 4/5, +1)

**File**: `src/gateway/routes.py`

- In `_load_enabled_mcps` (line 66) and `_get_all_published_mcps` (line 133), ensure `credit_cost_per_call` is included in the `select("*")` — already is via `*`, verify.
- In the `list_mcps` and `browse_mcps` branches of `call_tool_handler` (lines 232-249), include `"credit_cost_per_call": m.get("credit_cost_per_call", 0)` in each dict.

**Acceptance**: response payload for both tools includes the cost field; manual `curl` check.

---

## Step 4 — Rename `list_tools` → `list_mcp_tools`, `call_tool` → `invoke_mcp_tool` (Criterion 6, +3)

**Files**: `src/gateway/routes.py`, `README.md`, any docstrings.

1. Rename the two tool entries in `list_tools_handler` (lines 189-210).
2. Rename the corresponding branches in `call_tool_handler` (lines 299, 313).
3. **Deprecation shim**: for 1 release cycle, keep `list_tools` and `call_tool` as hidden aliases (not in `list_tools_handler` output, but accepted in `call_tool_handler` dispatch with a `_log_deprecated_alias` line). Rationale: any agent conversation mid-session that cached the old names keeps working.
4. Update README § "Gateway Meta-tools" table and `CHANGELOG.md`.
5. Remove the shim in the next minor release.

**Acceptance**: fresh `initialize` returns 7 tools with the new names; legacy names still work but are logged as deprecated.

---

## Step 5 — Structured output (`outputSchema`) (Criterion 2, +3)

**File**: `src/gateway/routes.py:149-211` and `call_tool_handler` returns.

The Python MCP SDK supports structured content via `CallToolResult.structured_content` alongside `TextContent`. For each tool, declare `outputSchema` in the `Tool(...)` definition and return both string and structured form.

Schemas (summary — write as JSON Schema in code):

| Tool | `outputSchema` |
|------|----------------|
| `list_mcps`, `browse_mcps` | `{ type: array, items: { slug, name, description, category, enabled?, credit_cost_per_call } }` |
| `add_mcp`, `remove_mcp` | `{ status: enum[added, removed, already_enabled, error], mcp?: string, name?: string, error?: string }` |
| `search_tools` | `{ type: array, items: { mcp, mcp_name, tool, description, inputSchema } }` |
| `list_mcp_tools` | `{ type: array, items: { name, description, inputSchema } }` or `{ error, reason }` |
| `invoke_mcp_tool` | `{ type: object }` (upstream-defined; stay loose) |

Update the return path from `return [types.TextContent(type="text", text=text)]` to the SDK's `CallToolResult(content=[...], structuredContent=<dict>)` where available, keeping the `TextContent` string for clients that don't handle structured output.

**Acceptance**: the `initialize`/`tools/list` response carries `outputSchema`; a test client receives structured JSON without needing to re-parse.

---

## Step 6 — Verification & rollout

1. **Local smoke**: start server, connect with `mcp-cli` or Claude Desktop, confirm 7 tools, confirm `initialize` response contains `instructions` and each tool has populated description + outputSchema.
2. **Regression**: exercise full lifecycle (`browse` → `add` → `search` → `invoke`) against a staging upstream. Confirm credit deduction still logs correctly (`_log_tool_call`).
3. **Re-audit**: re-run `mcp-audit` skill and record new scorecard in a second report.
4. **Changelog**: bump to `1.10.0` — "Gateway meta-tools: agent-ready descriptions, structured output, protocol-safe names".
5. **Deploy**: push to `main` → Railway auto-deploy.

---

## Commit plan

```
feat(gateway): add server-level instructions for agent orientation         # Step 1
docs(gateway): rewrite tool descriptions with when-to-use and examples     # Step 2
feat(gateway): surface credit_cost_per_call in list/browse_mcps            # Step 3
refactor(gateway): rename list_tools→list_mcp_tools, call_tool→invoke_…    # Step 4
feat(gateway): declare outputSchema + structured content on all tools      # Step 5
chore: changelog, README, bump to 1.10.0                                   # Step 6
```

Each commit is independently revertable. Suggested ordering above; feel free to parallelise Steps 1–3 since they touch disjoint code.

---

## Out of scope (deliberately)

- Criterion 1 is already 9/10 — no further abstraction work warranted; the `invoke_mcp_tool` passthrough is structurally necessary.
- Criterion 3 is 9/10 — do not add deferred-loading; 7 tools does not warrant it.
- Full REPL/code-execution affordance (the "perfect 10" for Criterion 2) is deferred — it would be a new major feature, not a fix.
