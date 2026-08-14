# seo-writer — Architecture

Public beta: local-first, Skill-driven and research-gated. Production research
uses user-configured adapters; deterministic mocks are reserved for tests and
demos. This document explains the component layout, state machine, approval
model, idempotency, and audit/cost model. `docs/AUDIT.md` covers audit
guarantees; `docs/MIGRATION.md` covers provider configuration.

## 1. Layer overview

```
┌─────────────────────────────────────────────────────────────┐
│ CLI (cli.py)  Typer tree · --json · exit codes 0/1/2        │
├─────────────────────────────────────────────────────────────┤
│ services.py  pipeline steps + approval + idempotency        │
│ state_machine.py  transition table + step authorization     │
│ workflow.py  canonical review artifacts + deterministic HTML│
├───────────────┬──────────────┬──────────────────────────────┤
│ db.py         │ facts/policy │ validators/                  │
│ SQLite        │ import +     │ research_gate (pure)         │
│ (runs, cmds,  │ snapshot     │ claim_safety (pure)          │
│  costs,       │ hashing +    │                              │
│  audits,      │ invalidation │                              │
│  approvals)   │              │                              │
├───────────────┴──────────────┴──────────────────────────────┤
│ providers/  ProviderResult · real adapters + test mocks    │
│             DataForSEO / Reddit / HTTP / agent-from-file   │
└─────────────────────────────────────────────────────────────┘
```

Design goals that shape everything:

- **Pure core, side-effect shells.** Validators and the gate are pure
  functions over evidence/corpus dictionaries; database and provider
  interactions live in `services.py` and `db.py`. This is what makes the
  acceptance tests fast and deterministic.
- **Every result is a `ProviderResult`.** Every provider call returns
  provider, profile, operation, request fingerprint, cost estimate, token
  estimate, retryability and source confidence — so costs, audits and
  evidence typing have one source of truth.
- **Idempotency keys everywhere.** Every pipeline step records its command
  under `UNIQUE(run_id, command, idempotency_key)`. Default keys
  (`run:{id}:{step}`) short-circuit; explicit/retry keys re-execute.

## 2. State machine

States and transitions are table-driven in `state_machine.py`:

```
created → researching → research_gate_passed → outline_pending_approval
        → approved → drafting → completed → exported
researching/drafting → blocked
blocked → researching            (evidence or provider remediation)
blocked → outline_pending_approval (approval remediation only)
```

Two tables:

- `TRANSITIONS` — which status transitions are legal (`assert_transition`).
- `STEP_AUTHORIZED` — which steps may run from which status
  (`assert_step_authorized`). Steps fail **loudly** with
  `StateTransitionError` when not authorized; there is no silent no-op.

Notable authorizations (these carry product meaning):

| step | authorized from | why |
|---|---|---|
| `research` | created, researching, gate_passed, outline_pending, blocked | re-research demotes/removes stale evidence |
| `validate_research` | researching, gate_passed, blocked | gate can be re-evaluated after remediation |
| `outline` | gate_passed, outline_pending, approved, drafting, completed, blocked | re-generating an outline is allowed but **invalidates approval** (AC6) |
| `gap_map` | researching, gate_passed, outline_pending, approved | validates current-run evidence and invalidates an existing approval |
| `render` | every state except created | read-only deterministic HTML rendering |
| `import_review` | outline_pending, approved | stale-safe import creates a new outline revision |
| `approve` | outline_pending, approved | re-approval binds the *latest* facts snapshot |
| `draft` | outline_pending, approved, drafting, completed, blocked | refusal on unapproved outline raises `ApprovalRequiredError` before any LLM call (AC4) |
| `metadata` | outline_pending, drafting, completed, blocked | same approval guard as draft |
| `validate` | every state except created | aggregates all rules; completes the run |
| `export` | completed, exported, blocked | only after validation passed |
| `retry` | every state except created | explicit, fresh-key re-execution |

## 3. Approval model

The approval gate is the product's spine:

```
outline.generated (rev N) → outline_pending_approval
approve_outline(rev N, approver) → binds facts snapshot hash → approved
```

- **Approvals bind to the latest facts snapshot hash.** When facts are
  re-imported (or policy is re-imported), every run whose approval hash no
  longer matches is **demoted to `outline_pending_approval`** and an
  `approval.invalidated` audit event is written. Drafting refuses with
  `ApprovalInvalidatedError`.
- **Approvals are per-revision and superseded, not deleted.**
  `supersede_approvals(run_id, before_revision)` stamps `superseded_at` on
  every older approval when a newer outline revision is generated — an audit
  trail of what was approved, by whom, against which facts, and when it
  stopped being current.
- **The run keeps its immutable brief/facts snapshot** (for manifest
  traceability) while the *live* approval always validates against the
  latest snapshot.
- **The refused draft path makes zero LLM calls** — the approval guard runs
  before provider construction (asserted in AC4 by counting provider calls).
- `run_validate` raises `ApprovalRequiredError` when no approved revision
  exists; a stale approval (facts changed) surfaces as a
  `ValidationFailedError` with the invalidation reason.

## 4. Idempotency & costs

- `command_ledger UNIQUE(run_id, command, idempotency_key)` — a step whose
  key was already recorded returns the prior result verbatim.
- `cost_ledger UNIQUE(idempotency_key, provider, operation)` — each
  provider operation is billed at most once per key. Transient retries keep
  the *same* key, so a retried operation is billed once (AC9).
- Default keys: `run:{run_id}:{step}`; `--idempotency-key` and `run retry`
  use fresh keys (`run:{run_id}:{step}:retry`) and intentionally re-cost
  (AC8).
- Provider errors are classified `TransientProviderError` vs
  `PermanentProviderError` with a `retryable` flag. Transient failures retry
  up to `policy.retries` with a `provider.retry` audit per attempt and never
  enter the cost ledger on failure; permanent failures block immediately,
  never retry, never bill (AC9).
- `policy.cost_limit_per_run` is enforced before each billed call.

## 5. Evidence typing

`db.list_evidence` rows carry `evidence_origin`:

- `current_run` — the page/thread was opened during this run (SERP pages,
  community threads with `opened_current_run=True`).
- `structured_discovery` — surfaced by a search/API step, not opened
  (keyword volume, SERP metadata from the API).
- `snippet_only` — only a snippet was captured; the page was never opened.
- `reused_prior_run_evidence` — carried over from an earlier run.

The gate counts only **opened, non-promotional** threads/SERP pages and only
in-run evidence; snippet-only and reused rows never satisfy floors (AC7).
`aio_visible: False` is a valid observation (the query was run and no AI
overview was present), not a missing field.

## 6. Validation chain

`run validate` aggregates everything that must hold before export:

1. an approved outline revision exists;
2. a draft exists with metadata;
3. research gate passed (re-checked from evidence rows);
4. claim safety: generic unsafe patterns (superlatives, guarantees,
   no-review language) + per-brand blocked wording;
5. material terms in copy map to approved claim ids — the error names the
   offending sentence;
6. metadata length caps (title ≤ 60, description ≤ 155, alt ≤ 125, slug
   pattern), outline structure markers, FAQ shape.

Any failure sets the run to `blocked` (step `validate`), writes
`validation.failed` with the full error list, and raises
`ValidationFailedError` — exit code 1. Validation failures are **blocking
and auditable, never silently downgraded**.

## 7. Export manifest

`run export` writes `article.md` + `manifest.json` into
`<data-dir>/<workspace>/objects/<run-id>/export/markdown/` (and optionally
copies both to `--out-dir`). The manifest carries:

- `rules_version`, `run_id`, brand slug, status;
- brief snapshot (immutable), facts hash + version;
- outline revision + approved revision + content sha256;
- approval (outline revision, approver, timestamp, facts hash);
- every evidence row (id, source type, fetch method, opened flag, origin);
- cost total and the full audit event list including `export.created`.

This is the traceability contract: any exported article can be walked back
to the facts, outline revision, approval, evidence and cost total that
produced it (AC10).

The additive HTML format writes `article.html` under `export/html/`. The
manifest records hashes for the article, content map, outline sidecar, and
outline review when those artifacts exist. Customer-facing HTML never renders
confidential review content.

## 8. Directory layout

```
~/.seo-writer/                     # default data root (SEO_WRITER_DATA)
└── <workspace>/                   # per-workspace (default "default")
    ├── seo-writer.db              # SQLite: brands, projects, runs, commands,
    │                              #   costs, audits, approvals, facts snapshots
    └── objects/
        └── <run-id>/
            ├── outlines/rev-N.md
            ├── draft.md
            ├── metadata.json
            ├── gap/content-map.json
            ├── gap/content-map.html
            ├── gap/opportunity-map.html
            ├── outlines/rev-N.json
            ├── outlines/rev-N.html
            ├── reviews/outline-rev-N.review.json
            └── export/markdown/
                ├── article.md
                └── manifest.json
```

## 9. Package layout

```
src/seo_writer/
  cli.py               Typer app; root flags; _guard maps errors → JSON + exit code
  services.py          pipeline steps; approval; idempotency; retry; cost limits
  state_machine.py     TRANSITIONS / STEP_AUTHORIZED / RETRY_RESUME tables
  db.py                SQLite schema + helpers (rows as dicts; audit payloads JSON)
  models.py            Pydantic models: FactsYaml, PolicyYaml, ResearchGatePolicy,
                       Brief; SKILL_GATE_FLOOR
  facts.py             import_facts: versioned snapshots, hash binding,
                       approval invalidation
  policy.py            import_policy: provider profiles, gate floors enforced
  ids.py               run ids, sha256_text, hash_payload, idempotency_key
  errors.py            SeoWriterError(1) / UsageError(2) / ValidationFailedError /
                       ApprovalRequiredError / ApprovalInvalidatedError /
                       TransientProviderError / PermanentProviderError
  validators/
    research_gate.py   evaluate(): pure gate over evidence rows
    claim_safety.py    check_unsafe_wording / check_material_claims /
                       validate_metadata_lengths / validate_outline_structure /
                       validate_run_corpus
  providers/
    base.py            ProviderResult (provider, profile, fingerprint, cost,
                       tokens, retryability, source_confidence)
    fixtures.py        load_fixture / pop_failures / fingerprint
    mock_keyword.py    deterministic volumes, PAA pool
    mock_serp.py       SERP rows + snippet-only variants
    mock_webfetch.py   opened pages + source map
    mock_community.py  thread rows across subreddits + second platform
    mock_llm.py        outline/draft/metadata templates; claim-safe by
                       construction; inject fixtures for failure-path tests
    real.py             user-configured DataForSEO, Reddit and HTTP adapters
```
