# seo-writer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Local-first, research-gated, claim-safe SEO content production CLI.

An opinionated pipeline that takes a topic brief through **research → gate →
outline → human approval → draft → metadata → validation → export**, where every
substantive product claim in the final copy is traceable to an approved entry
in the brand's facts ledger, and every blocking rule is enforced with an audit
trail — never silently downgraded.

Phase 1 MVP is **offline and deterministic** for article production: all five
provider roles (keyword / SERP / web-fetch / community / LLM) run on bundled
mock profiles — no paid API, no API keys. The only network touchpoint is
onboarding (`onboard fetch` crawls the customer's own website over plain
HTTP; `providers verify` pings the customer's own DataForSEO/Reddit
credentials). Real provider roles are a Phase 2 swap behind the same
interfaces (see `docs/MIGRATION.md`).

## Why this shape

- **The CLI is the product.** No SaaS, no web UI, no payment flow, no CMS
  publishing in Phase 1. The artifact is a directory of `article.md` +
  `manifest.json` you can hand to an editor or a CMS.
- **Research gate first.** An outline is never generated before the gate
  passes (3 queries, 5 opened SERP pages, 10 opened community threads across
  4 subreddits, second platform or documented insufficiency — floors equal to
  the `seo-blog-writer` Skill, and policies cannot weaken them).
- **No draft without explicit approval.** The outline sits in
  `outline_pending_approval` until a human approves the exact revision.
  Anything that invalidates that approval (facts update, policy update,
  outline re-generation) demotes the run and blocks drafting until
  re-approval. The refused path makes **zero LLM calls**.
- **Claims trace to the facts ledger.** Blocked claims never reach the draft,
  the FAQ, or the metadata. Material terms used in copy must have an approved
  claim-id entry; the validator names the offending sentence.
- **Idempotent by default.** Re-running a step with its default key
  short-circuits: no duplicated provider costs, no duplicated artifacts, no
  duplicated audit events. Explicit retries use fresh keys and re-cost.

## Quick start

```bash
# install (uv-managed)
cd seo-writer
uv sync

# smoke-test everything offline
uv run pytest tests/ -q
uv run ruff check .
```

Create a brand and run one article end to end:

```bash
export SEO_WRITER_DATA_DIR=~/.seo-writer     # optional; default is ~/.seo-writer
alias sw="uv run seo-writer --data-dir $SEO_WRITER_DATA_DIR"

sw init
sw brand create acme
sw project create acme blog

# import the *example* pack — replace with your customer's real facts first
sw brand facts import acme examples/brand-packs/generic-acme/facts.yaml
sw brand policy import acme examples/brand-packs/generic-acme/policy.yaml

sw run create acme blog --brief examples/brand-packs/generic-acme/topics/workflow-decisions.yaml
sw run research <run-id>
sw run validate-research <run-id>            # gate — blocks without evidence
sw run outline <run-id>                      # outline_pending_approval
sw run approve <run-id> --revision 1 --approver "you@example.com"
sw run draft <run-id>
sw run metadata <run-id>
sw run validate <run-id>                     # claim-safety + structure — completes the run
sw run export <run-id> --format markdown
```

Everything supports `--json` for scripting. Exit codes: `0` success,
`1` business failure (gate/approval/validation — JSON error on stderr),
`2` usage error.

## CLI reference

```
seo-writer init
seo-writer brand create <slug>
seo-writer brand list
seo-writer brand facts import <slug> <facts.yaml>
seo-writer brand facts show <slug>
seo-writer brand policy import <slug> <policy.yaml>
seo-writer brand policy show <slug>
seo-writer project create <brand> <slug>
seo-writer project list <brand>
seo-writer run create <brand> <project> --brief <topic.yaml>
seo-writer run list <brand> [--project <slug>]
seo-writer run research <run-id>
seo-writer run validate-research <run-id>
seo-writer run outline <run-id> [--from-file outline.md]
seo-writer run approve <run-id> --revision N --approver NAME
seo-writer run draft <run-id> [--from-file draft.md]
seo-writer run metadata <run-id> [--from-file metadata.yaml]
seo-writer run validate <run-id>
seo-writer run export <run-id> --format markdown [--out-dir DIR]
seo-writer run status <run-id>
seo-writer run evidence <run-id> [--json]
seo-writer run costs <run-id>
seo-writer run retry <run-id> --step research|outline|draft
seo-writer onboard site <brand> --url https://…
seo-writer onboard fetch <brand>
seo-writer onboard confirm <brand> --approver NAME
seo-writer onboard status <brand>
seo-writer providers configure [--name dataforseo|reddit]
seo-writer providers verify [--name dataforseo|reddit]
seo-writer providers status
```

Global flags sit before the subcommand: `--data-dir`, `--json`, `--version`.

## Onboarding a new brand

New-brand setup is a four-step journey; a ready-made agent wrapper lives in
`skills/seo-writer-onboarding/`:

1. **`onboard site <brand> --url https://…`** — record the customer's website
   as local brand memory (`brands/<slug>/site.yaml`). Interactive prompt when
   `--url` is omitted.
2. **`onboard fetch <brand>`** — crawl the site over plain HTTP (no key, no
   LLM), keep `index.html` + `content.txt` + `seo-audit.yaml` under
   `brands/<slug>/site-crawl/`, and run a category-scoped SEO audit scored
   0–100. The rule set is a Python re-implementation of the static-checkable
   subset of the MIT-licensed
   [seo-skills/seo-audit-skill](https://github.com/seo-skills/seo-audit-skill)
   (251 rules / 20 categories upstream; ~100 rules embedded here — see
   `src/seo_writer/seo_rules.py` and `NOTICE`): core / htmlval / social /
   content / a11y / images / url / mobile / i18n / security / technical /
   crawl / redirect / schema / eeat / geo / legal / perf, plus robots.txt +
   sitemap.xml checks fetched alongside the page (absence tolerated, reported
   as findings). Each rule is
   a pure standard-library check — no third-party runtime dependencies — with
   `pass(100)/warn(50)/fail(0)` semantics mapped to
   `ok(-0)/info(-2)/warning(-10)/error(-30)` and
   `score = max(0, 100 − errors×30 − warnings×10 − infos×2)`. Every
   `seo-audit.yaml` carries a `rubric` field naming the upstream source.
3. **`onboard confirm <brand>`** — an agent (or you) writes the product's
   feature summary to `brands/<slug>/site.md` from the crawled text; the
   customer reviews and edits it; `confirm` stamps who approved and when.
4. **`providers configure`** — interactive DataForSEO (login/password) and
   Reddit (client_id/client_secret) setup. Secrets go to a chmod-600
   `.secrets.yaml` in the data dir — never in git, never echoed — and every
   provider is verified live on configure (DataForSEO ping, Reddit OAuth).
   Env defaults are offered (DATAFORSEO_LOGIN/PASSWORD, REDDIT_CLIENT_ID/
   CLIENT_SECRET); Reddit honours `REDDIT_PROXY_URL`.

The confirmed feature summary seeds `facts.yaml` for the production pipeline.
Configured-but-unverified credentials block nothing yet — the real
search/community providers are a Phase 2 swap.

## Agent workflow — no LLM API needed

The CLI can run the whole pipeline with **you (or an agent) as the writer**:
outline, draft and metadata are authored as files and imported with
`--from-file`. The CLI skips its LLM provider entirely (zero calls, audit
events marked `origin: external`) while enforcing everything else — gate,
approval, claim safety, idempotency, revisioning.

```bash
seo-writer run research <run-id>
seo-writer run validate-research <run-id>        # gate must pass
# 1. write outline.md yourself (structure validated on import)
seo-writer run outline <run-id> --from-file outline.md
seo-writer run approve <run-id> --revision 1 --approver "editor@example.com"
# 2. write draft.md yourself (approved claim wording only)
seo-writer run draft <run-id> --from-file draft.md
# 3. write metadata.yaml yourself (title ≤60 / desc ≤155 / alt ≤125)
seo-writer run metadata <run-id> --from-file metadata.yaml
seo-writer run validate <run-id>                 # agent-authored copy is validated too
seo-writer run export <run-id> --format markdown
```

A ready-made agent wrapper lives in `skills/seo-writer/` (SKILL.md + content
templates): it spells out the content requirements, the claim rules and the
failure-handling table. Importing the same file twice is idempotent; editing
the file and re-importing creates a new outline revision and invalidates the
previous approval (re-approve).

## Data model

- **Brand** — isolated fact ledger + policy. Facts and policy are
  *imported* per brand; nothing customer-specific ships in the repo.
- **Project** — a topic bucket under a brand.
- **Run** — one ArticleRun. State machine:
  `created → researching → research_gate_passed → outline_pending_approval →
  approved → drafting → completed → exported`, with
  `researching/drafting → blocked` and `blocked → researching` (evidence or
  provider remediation) or `blocked → outline_pending_approval` (approval
  remediation only).
- **SQLite** at `~/.seo-writer/<workspace>/seo-writer.db` — runs, commands,
  costs, audits. **Objects** (outline revisions, draft, exported manifest)
  at `<data-dir>/<workspace>/objects/<run-id>/`.

Config holds provider *profile references* only; secrets never live in YAML —
Phase 2 real providers read them from env (`docs/MIGRATION.md`).

## Evidence typing

Every evidence row carries a strict origin, verified by tests:

| `evidence_origin` | meaning |
|---|---|
| `current_run` | opened and read during this run (SERP pages, threads) |
| `structured_discovery` | surfaced by an API/search step, not opened (keyword volume, SERP metadata) |
| `snippet_only` | only a snippet was captured, the page was never opened |
| `reused_prior_run_evidence` | carried over from an earlier run |

The gate counts *opened* evidence only; snippet-only and reused rows never
satisfy the floors.

## Acceptance tests

`tests/test_ac1..test_ac10` map 1:1 to the Phase 1 acceptance criteria:

| AC | file | guarantees |
|---|---|---|
| 1 | `test_ac1_offline_mock_run.py` | full happy path offline with mock providers |
| 2 | `test_ac2_brand_isolation.py` | facts/policy/claims/artifacts isolated per brand |
| 3 | `test_ac3_gate_failure_blocks.py` | gate failure blocks outline, gaps reported + audited |
| 4 | `test_ac4_unapproved_outline_blocks_draft.py` | unapproved outline blocks draft/metadata/validate, **zero LLM calls** on the refused path |
| 5 | `test_ac5_blocked_claims_excluded.py` | blocked claims never reach draft/FAQ/metadata; injected copy fails validation |
| 6 | `test_ac6_facts_change_invalidates_approval.py` | facts/policy/outline changes invalidate approval, demote the run |
| 7 | `test_ac7_evidence_typing.py` | strict current-run / structured / snippet / reused typing |
| 8 | `test_ac8_idempotency.py` | default-key reruns short-circuit; fresh-key retries re-cost |
| 9 | `test_ac9_transient_vs_permanent.py` | transient errors retried per policy; permanent errors never retried |
| 10 | `test_ac10_export_traceability.py` | manifest traces run, facts hash/version, rules version, outline revision + hash, approval, evidence ids, costs, audit events |

## Repository layout

```
src/seo_writer/
  cli.py                 # Typer command tree, --json output, exit-code mapping
  services.py            # pipeline steps, approval model, idempotency, retry
  state_machine.py       # transition table + step authorization
  db.py                  # SQLite (runs, commands, costs, audits, approvals)
  models.py              # Pydantic: FactsYaml / PolicyYaml / Brief / gate policy
  facts.py               # facts import + snapshot hashing + approval invalidation
  policy.py              # policy import + validation against Skill floors
  ids.py                 # run ids, sha256, idempotency keys
  onboard.py             # onboarding: site memory, crawl + SEO audit, provider config
  seo_rules.py           # SEO audit rules (~100, seo-audit-skill MIT subset; see NOTICE)
  validators/            # research_gate, claim_safety (pure, unit-testable)
  providers/             # ProviderResult + mock keyword/serp/webfetch/community/llm
docs/                    # ARCHITECTURE.md, AUDIT.md, MIGRATION.md
skills/                  # seo-writer (production) + seo-writer-onboarding (brand setup)
examples/brand-packs/    # generic-anonymous example pack (no customer facts)
tests/                   # AC1–AC10 + validator units, fixture-driven mocks
```

## Roadmap

- **Phase 1 (current)** — offline deterministic production pipeline
  (mock providers, audit/approval/claim-safety machinery, 75 tests) plus
  onboarding: site memory, website crawl + baseline SEO audit, confirmed
  feature summary, provider credential setup with live verification.
- **Phase 2 — bring your own data sources.** The 5 provider roles get real
  implementations (DataForSEO, SERP APIs, Reddit, OpenRouter/LLM) that *you*
  configure with *your* keys via `policy.yaml` + environment variables. The
  mocks remain the compatibility contract for tests. See
  `docs/MIGRATION.md` for the per-role checklist.
- **Phase 3 — commercial packaging** (license, usage metering) only when
  there is a user base to justify it. The codebase is built so a hosted
  gateway can be added without changing the CLI contract.

## Security posture (Phase 1)

- No secrets, keys, or credentials in the repo — ever.
- `~/.seo-writer` workspace is user-owned and gitignored.
- Facts/claims are **project-isolated per brand**; the example pack is
  deliberately anonymous. Never commit a customer's real facts ledger.
- Real providers in Phase 2: interfaces and config schemas only, secrets from
  env (`docs/MIGRATION.md` for the full checklist).
- Model weights / provider artifacts never live in the repo (see global
  `~/.cache/models/` convention in the developer environment rules).
