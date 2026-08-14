# seo-writer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Local-first, research-gated, claim-safe SEO content production CLI.

An opinionated pipeline that takes a topic brief through **research → gate →
outline → human approval → draft → metadata → validation → export**, where every
substantive product claim in the final copy is traceable to an approved entry
in the brand's facts ledger, and every blocking rule is enforced with an audit
trail — never silently downgraded.

The public product is an Agent Skill plus a local Python CLI. Production
research uses user-configured DataForSEO, Reddit and plain HTTP page fetching;
the bundled mock providers are only for tests and demonstrations. The Skill's
agent-authored `--from-file` workflow does not require a CLI LLM key. Real
provider calls happen only after the user manually configures and verifies
their own accounts in the data directory.

## Why this shape

- **The Skill + local CLI is the product.** The Skill governs the Agent
  workflow; the CLI enforces state, approval, claims, audit and export. There
  is no SaaS, web UI, payment flow or CMS publishing in this release.
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
# source checkout or extracted release bundle (uv-managed; no PyPI)
cd seo-writer
uv sync --frozen
./bin/seo-writer --version

# smoke-test everything offline
uv run pytest tests/ -q
uv run ruff check .
```

Create a brand and run one article end to end:

```bash
export SEO_WRITER_DATA_DIR=~/.seo-writer     # optional; default is ~/.seo-writer
alias sw="./bin/seo-writer --data-dir $SEO_WRITER_DATA_DIR"

sw init
sw brand create acme
sw project create acme blog

# import the *example* pack — replace with your customer's real facts first
sw brand facts import acme examples/brand-packs/generic-acme/facts.yaml
sw brand policy import acme examples/brand-packs/generic-acme/policy.yaml

# production: configure the user's own provider accounts before research
sw providers configure --name dataforseo
sw providers configure --name reddit
sw providers status
# import a policy selecting dataforseo + reddit + http (see the template)

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

For the curated source bundle, installation, upgrade, rollback and release
gates, see [`docs/RELEASE.md`](docs/RELEASE.md). SEO Writer is not published to
PyPI.

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
seo-writer gsc setup <brand>                          # credential state + guidance
seo-writer gsc auth <brand> [--no-launch-browser] [--gcloud]
seo-writer gsc sites <brand>
seo-writer gsc connect <brand> --property <url>       # bind property to brand
seo-writer gsc pull <brand> [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--force] # incremental, idempotent
seo-writer gsc inspect --brand <brand> --url <url>  # uses the bound property
seo-writer gsc import <brand> <gsc.csv>                         # dated GSC UI export
seo-writer gsc insights <brand> [--window 28]
seo-writer --json gsc status --brand <brand>   # --json is a global flag
```

GSC integration overview (zero third-party runtime deps, stdlib only):

- **`setup`** — three auth paths: A. gcloud ADC (preferred), B. own OAuth
  client json (`import` via `setup` prompt, PKCE loopback flow via `auth`),
  C. GSC UI CSV export fallback via `import`.
- **`pull`** — Search Analytics (date+query / date+page dimensions),
  25,000-row startRow pagination, incremental upsert skipping synced dates
  (repeat runs make **zero** API calls; `--force` re-pulls), 429/5xx
  exponential backoff (1s→60s + jitter) and a 1,200 QPM rate limiter.
- **`insights`** — high-impressions/low-CTR queries, rising queries,
  URL performance vs the `onboard fetch` audit baseline.
- GSC is read-only: SEO Writer can inspect Search Console data and read local
  or existing sitemap information, but never submits or changes a Sitemap.
  Search Analytics exposes the most important rows subject to Google's
  internal limits; results are not a complete keyword database. CSV fallback
  imports must contain a real `Date` column and are labeled `csv-import`.
- Credentials and GSC data stay on the customer machine
  (`--data-dir/.../gsc/<brand>/`, token files chmod-600); nothing is sent to
  third parties. See the GSC sections in `docs/ARCHITECTURE.md` for the design.

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
3. **`onboard confirm <brand>`** — an agent (or you) writes an evidence-backed
   product brief to `brands/<slug>/site.md` from the crawled text. It must
   cover target audience; FAB (Feature, Advantage, Benefit); limitations and
   non-capabilities; competitor context; unsupported claims; and open
   questions. The customer reviews and edits it; `confirm` rejects missing or
   empty sections, then stamps who approved and when.
4. **`providers configure`** — interactive DataForSEO (login/password) and
   Reddit (client_id/client_secret) setup. Secrets go to a chmod-600
   `.secrets.yaml` in the data dir — never in git, never echoed — and every
   provider is verified live on configure (DataForSEO ping, Reddit OAuth).
   Env defaults are offered (DATAFORSEO_LOGIN/PASSWORD, REDDIT_CLIENT_ID/
   CLIENT_SECRET); Reddit honours `REDDIT_PROXY_URL`. Production research
   refuses to silently fall back to mock data when these providers are absent.

The confirmed product evidence brief seeds `facts.yaml` for the production pipeline.
The real search/community providers are selected by `policy.yaml`; configure
and verify them before `run research`.

### Content gap and local HTML review

After the research gate passes, import a current-run evidence-backed content
map and render the local review reports:

```bash
seo-writer --data-dir <dir> --workspace <workspace> --json run gap-map <run-id> --from-file content-map.json
seo-writer --data-dir <dir> --workspace <workspace> --json run render <run-id> --view content-map
seo-writer --data-dir <dir> --workspace <workspace> --json run render <run-id> --view opportunities
seo-writer --data-dir <dir> --workspace <workspace> --json run render <run-id> --view outline
seo-writer --data-dir <dir> --workspace <workspace> --json run import-review <run-id> outline-review.json
seo-writer --data-dir <dir> --workspace <workspace> --json run export <run-id> --format html
```

HTML is a self-contained customer review surface. JSON, Markdown, and YAML
remain canonical. Coverage and opportunity graphics render only when enough
deterministic data exists; every graphic has a table or text fallback.

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

Config holds provider *profile references* only; secrets never live in YAML.
Production adapters read credentials from the user-owned data directory
(`docs/MIGRATION.md`).

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
  providers/             # ProviderResult + real adapters and test-only mocks
docs/                    # ARCHITECTURE.md, AUDIT.md, MIGRATION.md
skills/                  # seo-writer (production) + seo-writer-onboarding (brand setup)
examples/brand-packs/    # generic-anonymous example pack (no customer facts)
tests/                   # AC1–AC10 + validator units, fixture-driven mocks
```

## Roadmap

- **Current public beta** — Skill onboarding, user-configured DataForSEO and
  Reddit research, HTTP page evidence, GSC customer-data workflows, and the
  local governance pipeline. Mocks remain the compatibility contract for
  tests and demos.
- **Future** — optional direct LLM provider support and additional search or
  community adapters. The agent-authored file workflow is already supported.
- **Phase 3 — commercial packaging** (usage metering) only when
  there is a user base to justify it. The codebase is built so a hosted
  gateway can be added without changing the CLI contract.

## Security posture

- No secrets, keys, or credentials in the repo — ever.
- `~/.seo-writer` workspace is user-owned and gitignored.
- Facts/claims are **project-isolated per brand**; the example pack is
  deliberately anonymous. Never commit a customer's real facts ledger.
- Real provider credentials are entered by the customer and stored only in the
  chmod-600 data directory; they are never in policy YAML, artifacts, logs or
  this repository. See `docs/MIGRATION.md` for provider details.
- Model weights / provider artifacts never live in the repo (see global
  `~/.cache/models/` convention in the developer environment rules).
