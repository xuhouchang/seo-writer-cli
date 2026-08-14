# seo-writer — Architecture Decision Records

Lightweight decision log for the seo-writer codebase. Each record states the
context, the decision, and the consequences we accept. New decisions append a
new ADR; superseded decisions are marked `Superseded by ADR-NNN` instead of
being edited in place.

| ADR | decision | status |
|---|---|---|
| 001 | Local-first CLI + Agent Skill is the product (no SaaS, no PyPI) | Accepted |
| 002 | Research gate blocks outline generation | Accepted |
| 003 | Human approval binds a facts snapshot; refusal costs zero LLM calls | Accepted |
| 004 | Every material claim traces to an approved facts-ledger entry | Accepted |
| 005 | Idempotency keys make every pipeline step re-runnable by contract | Accepted |
| 006 | Every decision that matters is audited; no silent failure path | Accepted |
| 007 | Exit-code contract 0/1/2 with JSON errors on stderr | Accepted |
| 008 | Pure core, side-effect shells | Accepted |
| 009 | Deterministic mocks for tests; real providers only after user config | Accepted |
| 010 | GSC integration is stdlib-only and local-first | Accepted |
| 011 | `cli.py` split into a `cli/` package with thin-shell `_guard` commands | Accepted |
| 012 | `gsc.py` split into a `gsc/` package with package-namespace runtime resolution | Accepted |
| 013 | CI enforces a ≥70% coverage gate | Accepted |

---

## ADR-001 — Local-first CLI + Agent Skill is the product (no SaaS, no PyPI)

**Status:** Accepted

**Context:** The release had to choose a delivery shape. A web UI or SaaS
would have required hosting, accounts and a CMS — none of which the
production workflow needs, and all of which would leak customer facts and
credentials into a third-party surface.

**Decision:** The product is an Agent Skill plus a local Python CLI,
distributed as a curated source bundle (uv-managed, never published to
PyPI). There is no SaaS, web UI, payment flow or CMS publishing in this
release. The Skill governs the Agent workflow; the CLI enforces state,
approval, claims, audit and export.

**Consequences:** Customers run everything on their own machine; credentials
and content never transit a service we operate. Updates are explicit
bundle releases with install/upgrade/rollback documentation
(`docs/RELEASE.md`). The Skill cannot independently enforce CLI invariants
(such as subprocess timeouts) — those remain host/launcher capabilities and
are not claimed otherwise.

## ADR-002 — Research gate blocks outline generation

**Status:** Accepted

**Context:** Ungated outlines produce articles with no evidentiary
underpinning, and permissive defaults invite silent quality erosion.

**Decision:** An outline is never generated before the research gate passes:
3 queries, 5 opened SERP pages, 10 opened community threads across 4
subreddits, a second platform or documented insufficiency. Floors are
enforced by `models.SKILL_GATE_FLOOR` and a policy cannot weaken them
(`validators/research_gate.py`). The gate counts only opened, non-promotional,
in-run evidence; snippet-only and reused rows never satisfy floors.

**Consequences:** The first `run outline` on thin research fails loudly
(exit 1, `research_gate.failed` audit with the full gap list). Re-research
after a failure is the only way forward; there is no `--force` that silently
skips the gate.

## ADR-003 — Human approval binds a facts snapshot; refusal costs zero LLM calls

**Status:** Accepted

**Context:** Auto-generated drafts that bypass human review are the central
risk this tool exists to remove, and a failed approval attempt should not
burn provider budget.

**Decision:** The outline sits in `outline_pending_approval` until a human
approves the exact revision with an approver identity. The approval binds the
latest facts/policy snapshot hash; any re-import that changes the snapshot
demotes affected runs and writes `approval.invalidated`. Approvals are
per-revision and superseded, never deleted. The refused draft path raises
`ApprovalRequiredError` **before** any provider is constructed — it makes
zero LLM calls.

**Consequences:** Approval is an audit object (revision, approver, timestamp,
facts hash), and every exported manifest can prove which revision was
approved by whom against which facts. A stale approval surfaces as a
validation failure naming the invalidation reason, never as a silent pass.

## ADR-004 — Every material claim traces to an approved facts-ledger entry

**Status:** Accepted

**Context:** Marketing copy that asserts unverified product facts is the
second central risk. Blocking rules were historically downgradeable, which
eroded trust.

**Decision:** Blocked claims never reach the draft, the FAQ or the metadata.
Material terms used in copy must map to an approved claim id; the validator
names the offending sentence. Every blocking rule is enforced with an audit
trail — never silently downgraded.

**Consequences:** Copy validation (`run validate`, `validators/claim_safety.py`)
is a hard gate before `run export`. A claim-safety failure blocks the run,
writes `validation.failed` with the full error list, and exits 1.

## ADR-005 — Idempotency keys make every pipeline step re-runnable by contract

**Status:** Accepted

**Context:** Re-running a step must not duplicate provider costs, artifacts
or audit events; but explicit retries must actually re-execute.

**Decision:** Every pipeline step records its command under
`UNIQUE(run_id, command, idempotency_key)`. Default keys
(`run:{id}:{step}`) short-circuit and return the prior result verbatim;
`--idempotency-key` and `run retry` use fresh keys
(`run:{id}:{step}:retry`) and intentionally re-cost. Transient provider
retries keep the same key and are billed at most once; failed transient
attempts never enter the cost ledger.

**Consequences:** Repeat runs make zero duplicate calls (asserted in
acceptance tests, e.g. a fully-skipped GSC pull making zero HTTP requests).
Retries are explicit and audited (`retry.research` / `retry.outline` /
`retry.draft`).

## ADR-006 — Every decision that matters is audited; no silent failure path

**Status:** Accepted

**Context:** Post-hoc review must be able to prove what was *refused* and
why, not just what passed.

**Decision:** The `audit` table records JSON payloads for every meaningful
event — gate pass/fail, approvals and their invalidation, retries, blocking,
validation pass/fail, exports. Failures are blocking and audited; there is no
silent failure path. `rules_version` is stamped on gate, outline, validation
and export events so an article can be attributed to the exact rule set.

**Consequences:** `docs/AUDIT.md` documents the event schema; the export
manifest embeds the full audit event list for the run, making every exported
article walkable back to facts, approvals, evidence and costs.

## ADR-007 — Exit-code contract 0/1/2 with JSON errors on stderr

**Status:** Accepted

**Context:** The Skill hosts drive the CLI as a subprocess and must
distinguish success from business failure from usage errors programmatically,
in both human and `--json` modes.

**Decision:** Exit codes are `0` success, `1` business failure
(gate/approval/validation), `2` usage error. Business failures emit JSON on
stderr (`{"error": ..., "message": ...}`) even in non-`--json` mode; usage
errors are mapped from Click/Typer exceptions the same way. The error
hierarchy in `errors.py` (`SeoWriterError(1)` / `UsageError(2)` / …) is the
single source of the contract.

**Consequences:** All commands funnel through the `_guard` wrapper so the
mapping is uniform; the launcher clears host-venv noise so stderr stays
parseable. This contract is asserted by release smoke tests and CLI
contract tests.

## ADR-008 — Pure core, side-effect shells

**Status:** Accepted

**Context:** The acceptance suite must be fast and deterministic, and the
validation logic must be testable without a database or network.

**Decision:** Validators and the research gate are pure functions over
evidence/corpus dictionaries. Database and provider interactions live in
`services.py` and `db.py`; every provider call returns a `ProviderResult`
(provider, profile, operation, fingerprint, cost/token estimates,
retryability, source confidence) so costs, audits and evidence typing share
one source of truth.

**Consequences:** Unit-testing validators requires no fixtures beyond plain
dicts; provider behavior is isolated behind the `ProviderResult` contract,
which also makes the mock/real provider switch (ADR-009) a pure
configuration decision.

## ADR-009 — Deterministic mocks for tests; real providers only after user config

**Status:** Accepted

**Context:** Real keyword/SERP/community/LLM calls are expensive, rate-limited
and non-deterministic; the release must be testable offline and must not
pretend synthetic data is real market research.

**Decision:** The bundled `mock_*` providers are deterministic and reserved
for tests and demos. Real research (DataForSEO keyword/SERP, Reddit +
Stack Exchange community, plain HTTP page fetch) activates only after the
user configures and verifies their own accounts (`providers configure` /
`verify`); missing credentials produce a uniform
`providers configure --name <provider>` hint. The example brand pack ships
with a mock policy, so the whole from-scratch flow runs offline with zero
configuration.

**Consequences:** Tests never hit real endpoints (`external_requests=0` in
release smoke). Product claims distinguish synthetic demo output from
real-provider output; GSC data is only real after explicit OAuth or dated
CSV import.

## ADR-010 — GSC integration is stdlib-only and local-first

**Status:** Accepted

**Context:** Adding google-api-python-client / google-auth-oauthlib pulls a
large dependency tree and pushes credential handling into third-party
libraries.

**Decision:** The Search Console integration uses only the standard library
(urllib / hashlib / base64 / http.server / csv / sqlite3) and implements its
own PKCE loopback flow, token refresh and 401-retry. Credentials stay on the
customer's machine: the gcloud ADC file is read in place; a self-built client
writes chmod-600 client json + token files under the workspace. Endpoint URLs
can be redirected via `SEO_WRITER_GSC_*` environment variables (CLI test
harness) or explicit function arguments (unit tests).

**Consequences:** No extra runtime deps; credential material never enters
git, logs or error messages (audit events carry only property references and
file paths). The integration is read-only — it inspects Search Console data
and reads local/existing sitemap info but never submits or changes a sitemap.

## ADR-011 — `cli.py` split into a `cli/` package with thin-shell `_guard` commands

**Status:** Accepted (2026-08-14, release remediation)

**Context:** `cli.py` grew to ~1000 lines. The bulk was Typer parameter
annotations — the command bodies are thin closures — which made the file hard
to navigate and review, and pushed the exit-code mapping into one `_guard`.

**Decision:** Split into `src/seo_writer/cli/` — `_common.py` (Ctx, state,
`_guard`, `_render`, shared helpers), `__init__.py` (app tree, `main()`,
`init`, command registration at the bottom), and one module per command
group (`onboard`, `providers`, `brand`, `project`, `run`, `gsc_cmd`).
Entry points are unchanged: `seo_writer.cli:main` (pyproject script) and
`__main__.py` import the same package. Command names come from explicit
`@app.command("name")` decorators. The module is named `gsc_cmd` (not `gsc`)
so `from .. import gsc` unambiguously refers to the `seo_writer.gsc` service
module. `run as run_cmd` avoids shadowing the `init` closure's `run`.

**Consequences:** `--help` output is byte-identical to the pre-split file
(asserted by diff during the split). The exit-code/`--json` contract
(ADR-007) is untouched. `cli.py` paths in tests/docs were updated to
`cli/__init__.py` (e.g. the release-bundle path assertion).

## ADR-012 — `gsc.py` split into a `gsc/` package with package-namespace runtime resolution

**Status:** Accepted (2026-08-14, release remediation)

**Context:** `gsc.py` was a 1495-line module spanning constants, errors,
credentials, HTTP transport, backoff, pull, CSV import, insights and OAuth.
Splitting it into submodules changes how monkeypatching works: tests patch
`gsc.<name>` on the **package namespace**, but submodules that import
constants at module load (`from ._constants import TOKEN_URL`) would snapshot
the old values and silently ignore the patch — a behavioral break.

**Decision:** Split into `gsc/` submodules (`_constants`, `_errors`, `_auth`,
`_http`, `_backoff`, `_pull`, `_csv`, `_insights`, `_oauth`) with the full
public API re-exported from `gsc/__init__.py` (`__all__`). Submodules resolve
the seven monkeypatchable names (`TOKEN_URL`, `TOKENINFO_URL`, `API_BASE`,
`GCLOUD_ADC_PATH`, `ROW_LIMIT`, `load_credentials`, `_http_request`) **at call
time through the package namespace** (`from .. import gsc as _gsc`), exactly
like the pre-split single module. `gsc/__init__.py` adds a PEP 562
`__getattr__` fallback so names not re-exported (e.g. the private
`_http_request`) still resolve to their owning submodule.

**Consequences:** `tests/test_gsc.py` monkeypatches work unchanged (91 tests
green) — the test-suite's contract is "patch `seo_writer.gsc.<name>`, observed
at call time". New submodules must follow the same rule: never snapshot a
monkeypatchable constant at import time. Non-constant, never-patched values
(e.g. `SCOPE`, `FRESHNESS_DELAY_DAYS`) may keep import-time binding.

## ADR-013 — CI enforces a ≥70% coverage gate

**Status:** Accepted (2026-08-14, release remediation)

**Context:** Release quality needs a floor: regressions that delete or bypass
tested paths should fail CI rather than quietly lower coverage.

**Decision:** The test suite runs under pytest-cov with
`--cov-fail-under=70` in CI (and locally). `pytest-cov` is pinned in
`pyproject.toml` and locked in `uv.lock`.

**Consequences:** CI fails when coverage drops below 70%. Current measured
coverage is ~82%, leaving headroom while still making a large drop visible.
Tests run file-by-file in constrained environments (a full-file batch can
OOM), which is a runner concern, not a coverage-gate concern.
