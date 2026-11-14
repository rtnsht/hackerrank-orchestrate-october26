# Ghost Data Still Exists — Mastra Deletion-Audit Agent

HackerRank Orchestrate (October 2026). For every request in `dataset/deletion_requests.csv` the agent decides whether
deletion was actually requested, reconstructs who the person is (current **and** historical identifiers), finds every
record, data flow and backup that still ties to them, rebuilds each record's state at `audit_at` from evidence (never
from workflow claims), applies `dataset/deletion_rules.csv`, and emits `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE`
with an ordered `FINDING → REMEDIATION → VERIFICATION` plan in `output.csv`.

The whole solution is **TypeScript on [Mastra](https://mastra.ai)** (`@mastra/core` agents, tools and workflows). The
challenge contract is in [`problem_statement.md`](./problem_statement.md); agent-session rules are in
[`AGENTS.md`](./AGENTS.md).

---

## Quick start

### Requirements

- Node.js **22.13+** (uses the built-in `node:sqlite` to execute the SQL snapshot exports)
- npm (packages are installed from the npm registry; no other downloads — the OCR language model ships as an npm package)

### Install and run

```bash
cd code
npm install
npm start
```

`npm start` audits all 518 requests through the Mastra workflow and writes **`output.csv` at the repository root**
(header `case_id,deletion_status,steps_json`, one row per case, sorted by case number). It validates every row against
the output contract (exits non-zero on any violation) and regenerates `code/evaluation/usage_report.md` for that run.
A full run takes a few seconds (first run ~30 s extra if the OCR cache is missing).

### Other commands

| Command (inside `code/`) | What it does |
| --- | --- |
| `npm run samples` | Runs the 32 public sample requests (predictions in `code/.runs/`) |
| `npm run evaluate` | Scores samples against `dataset/sample_output.csv` and the participant-created cases, rewrites the metrics block of `code/evaluation/evaluation_report.md` |
| `npm start -- --trace .runs/trace.jsonl` | Also writes a per-case explanation trace (linked records, link evidence, state evidence, rule applied) |
| `npm run llm-smoke` | Drives the real Mastra agent code paths with Mastra’s mock model (no key needed) to prove the LLM wiring |
| `npm run typecheck` | `tsc --noEmit` over the project |

### Optional LLM agents (environment variables only — never commit keys)

The decision engine is deterministic and needs **no API key**. The three Mastra LLM agents are configured
**independently**, so each can use a different provider, model, key or endpoint. Start from the sample file:

```bash
cd code
```

```bash
cp .env.example .env
```

`code/.env` is git-ignored and loaded automatically (shell variables take precedence). Then set, per agent
(`<AGENT>` = `INTENT`, `VISION` or `REVIEW`):

| Variable | Effect |
| --- | --- |
| `AUDIT_<AGENT>_ENABLED` | `auto` (run when credentials exist; default for INTENT and VISION), `true`, `false` (default for REVIEW) |
| `AUDIT_<AGENT>_MODEL` | Mastra model-router id, default `anthropic/claude-haiku-5-5` (INTENT) and `anthropic/claude-sonnet-5-5` (VISION, REVIEW) |
| `AUDIT_<AGENT>_API_KEY` / `AUDIT_<AGENT>_BASE_URL` | key / OpenAI-compatible endpoint for that agent only |
| `AUDIT_<AGENT>_TEMPERATURE`, `AUDIT_<AGENT>_MAX_OUTPUT_TOKENS`, `AUDIT_<AGENT>_PRICE_*_PER_MTOK` | generation settings and price override for the usage report |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, … | provider-wide keys, used by every agent whose model id has that provider prefix |
| `AUDIT_LLM=off` | force the deterministic path for every agent |
| `DATASET_DIR` | audit a different dataset directory with the same layout (used by the custom evaluation cases) |

`npm start` prints each agent's resolved configuration (never the key), and `evaluation/usage_report.md` records it
together with calls, tokens and cost. `npm run llm-smoke` proves the agent wiring with Mastra's mock model, no key
needed. Full reference: [`code/docs/ARCHITECTURE.md` §5.5](./code/docs/ARCHITECTURE.md#55-configuring-the-llm-agents-codeenv).

---

## Architecture

Full architecture, data-flow and sequence diagrams (with AI/LLM touch points highlighted) and local-run guide:
[`code/docs/ARCHITECTURE.md`](./code/docs/ARCHITECTURE.md).

```text
deletion_requests.csv ──► Mastra workflow "deletion-audit" (one run per request)
                            1 classify-intent      intent agent (LLM, optional) + multilingual rules
                            2 resolve-identity     current snapshot + user_audit_log history
                            3 discover-resources   identifier matching, trace correlation, catalog links,
                                                   source-record refs, deletion-worker batch reconciliation
                            4 reconstruct-state    point-in-time state at audit_at from evidence (claims ignored)
                                                   + deletion_rules.csv policy
                            5 plan-steps           ordered findings / remediation / verification + contract check
                                    │
evidence/ (SQL, JSON, logs, txt, PDF, PNG) ──► evidence index (normalised observations + snapshot scopes)
```

| Layer | Files |
| --- | --- |
| Mastra instance, agents, prompts | `code/src/mastra/index.ts`, `code/src/mastra/agents/index.ts`, `code/src/mastra/prompts/*.md` |
| Mastra tools (one per audit stage) | `code/src/mastra/tools/audit-tools.ts` |
| Mastra workflow | `code/src/mastra/workflows/deletion-audit-workflow.ts` |
| Evidence parsers | `code/src/core/evidence/` — `structured.ts` (SQL via `node:sqlite`, JSON bundles/exports/record lists, handover text), `logs.ts` (six log dialects, trace-id correlation), `media.ts` (pdf-parse, tesseract.js OCR with upscaling, screen-aware interpretation), `records.ts` (record → state) |
| Identity, linking, state, policy, plan | `code/src/core/identity.ts`, `audit.ts`, `reconcile.ts`, `policy.ts`, `validate.ts` |
| CLI and reports | `code/src/main.ts`, `code/src/report/usage-report.ts` |
| Evaluation | `code/evaluation/evaluate.ts`, `score.ts`, `custom-cases.ts`, `evaluation_report.md`, `usage_report.md` |

### Key decisions

- **Evidence over claims.** `reported_status`, deletion-worker results, sync and index activity are claims. State comes
  from snapshots, lookups, logs, screenshots and documents; only explicit checks (connector checks, restore-access
  checks, restore jobs) are treated as observations.
- **Point in time.** State uses only evidence items captured by `audit_at` and observed after `requested_at`; later
  exports may still *attribute* a record to the subject (ownership is time-invariant) but never decide its state and
  are never cited.
- **Copies count.** A record present in any replica / failover / regional copy is a remaining record (replication drift).
- **Enumerating snapshots prove absence** only for records with no explicit post-request sighting, preferring snapshots
  that compare more copies.
- **Identifier-less records** (an anonymised analytics profile, a primary profile looked up by id only) are attributed by
  reconciling the deletion-worker batch around `reported_at`, excluding anything other evidence ties to someone else.
- **Policy** is read from `deletion_rules.csv`; retained records (security audit within retention, active legal holds)
  are verified present; tombstones keep only the stable `user_id`.

See `code/evaluation/evaluation_report.md` for metrics, custom cases, failures found and improvements made, and
`code/evaluation/usage_report.md` for token usage and cost.

---

## Packaging the submission

From the repository root:

```bash
zip -r code.zip README.md code -x "*/node_modules/*" "*/.runs/*" "*/__pycache__/*" "*/.venv/*" "*/.git/*" "*/.DS_Store"
```

`code.zip` then contains the runnable solution, prompts, configuration, this README and `code/evaluation/` (with
`evaluation_report.md` and `usage_report.md`). `dataset/`, `node_modules/` and run outputs are excluded. Upload
`output.csv` and `log.txt` (chat transcript) separately.

Challenge and submission page:
https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
