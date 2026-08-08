# seo-writer — Phase 2 migration checklist (real providers)

Phase 1 ships every provider role as a deterministic mock. The interfaces,
config schemas and security stubs are already in place; swapping in real
providers is a per-role exercise behind `ProviderResult`. This document is
the checklist for that migration — it is deliberately conservative: the
first rule is that **secrets never enter YAML, git, logs or error messages**.

## 1. What is already in place

- **5 provider protocols** with a uniform result envelope
  (`ProviderResult`: provider, profile, operation, request fingerprint,
  cost estimate, token estimate, retryability, source confidence).
- **Policy-declared profiles**: `policy.yaml` declares
  `providers.<role>.name/profile` only — no keys.
- **Retry/block classification**: `TransientProviderError` /
  `PermanentProviderError` drive the retry policy and the blocked state.
- **Cost ledger**: every billed operation records one row per idempotency
  key; `cost_limit_per_run` is enforced before each call.
- **Fixture-driven failure injection** for tests (`fixtures/*.yaml`).
- **Idempotency**: providers are called once per key; re-runs short-circuit.

## 2. The security rules (non-negotiable)

1. **No secrets in config.** `policy.yaml` may reference a provider profile
   (`name`, `profile`, `base_url`) but never an API key. Keys come from the
   environment (`os.environ`) or a user-owned secret store, resolved at
   call time. If a provider needs a file-based credential (e.g. a service
   account JSON), the YAML references a path *outside the repo* and the
   file is never committed.
2. **No secrets in artifacts.** The manifest, metadata.json and audit
   payloads contain provider *names* and request fingerprints — never
   tokens, headers or keys. Review `request_fingerprint` implementations to
   ensure they hash URLs/params only.
3. **No secrets in errors.** `ProviderError` messages must name provider +
   operation, not the failing credential or request body. Log redaction:
   strip `Authorization`-style headers before any `logger.debug`.
4. **Facts stay project-isolated.** Real customer facts/claims are imported
   per brand at runtime from user-owned files (`brand facts import
   <customer-facts.yaml>`). Never add customer data to this repo, its
   fixtures, or any default value in code.
5. **Model weights / provider artifacts** follow the developer-environment
   convention: `~/.cache/models/`, never inside the repo.

## 3. Provider role checklist

### Keyword (DataForSEO)

- [ ] Implement `search_volume` over the DataForSEO keyword endpoint.
- [ ] Map API errors: 429/5xx → `TransientProviderError` (retryable),
      4xx auth/validation → `PermanentProviderError`.
- [ ] Profile `base_url` + env var (e.g. `DATAFORSEO_USERNAME` /
      `DATAFORSEO_PASSWORD`) resolved from `os.environ` at call time.
- [ ] Fingerprint = normalized query + location + device; never the auth
      header.
- [ ] Map PAA pool from the response `people_also_ask`.

### SERP

- [ ] Implement `search` over the chosen SERP API (or self-hosted fetch).
- [ ] **Strictly distinguish `opened_current_run` vs `snippet_only`** —
      a snippet-only row must never satisfy gate floors (AC7).
- [ ] AI-overview visibility: capture `aio_visible` as a real observation
      (including `False` when the AI overview was absent).
- [ ] Rate limits per policy; retries on transient codes only.

### WebFetch

- [ ] Implement `fetch_page` with a per-run budget (pages opened),
      robots/politeness, timeouts.
- [ ] Evidence rows must record `fetch_method` truthfully
      (`snippet_only` vs full page).
- [ ] Never persist raw page bodies in the DB — extract the source map +
      facts, not the HTML.

### Community (Reddit + second platform)

- [ ] Implement `search_threads` (Reddit) with `grade` classification:
      `high` / `low` / `promotional`. Promotional rows must be excluded
      from gate counts exactly like the mocks.
- [ ] Second-platform search (Quora / StackExchange / forums) with the
      documented-insufficiency path (`second_platform_search_outcome`).
- [ ] Respect rate limits; treat platform 5xx as transient.

### LLM (OpenRouter or direct)

- [ ] Implement `generate_outline` / `generate_draft` / `generate_metadata`
      with the *same* prompts and claim constraints as the mocks: the draft
      template must quote **all approved claims** and never emit blocked
      wording.
- [ ] `cost_estimate` from the real token/price model — the cost ledger
      must stay truthful per operation.
- [ ] Timeouts → `TransientProviderError`; auth/400 → permanent.
- [ ] **No prompt leakage**: never paste the facts ledger into a log; the
      request to the provider is a normal payload, but do not log it.
- [ ] Deterministic tests keep using the mocks; real-provider E2E tests are
      opt-in behind an env flag (e.g. `SEO_WRITER_E2E=1`) and skip in CI
      without keys.

## 4. Suggested order

1. **Keyword** — cheapest to validate (single endpoint, clear error codes).
2. **SERP + WebFetch** — this is where evidence typing quality lives;
      keep the gate tests green with snippet-only injection.
3. **Community** — grade classification + second-platform insufficiency.
4. **LLM** — last; it must sit behind an approved outline (the approval
      gate already prevents drafts without approval, so a broken LLM can
      only waste money, not emit unapproved copy — but the cost limit and
      idempotency still protect you).

## 5. Regression gate (must stay green after each swap)

```
uv run pytest tests/ -q
uv run ruff check .
```

Every provider swap must keep AC1–AC10 green. The fixtures
(`tests/fixtures/`) are the compatibility contract: mock providers must keep
producing the exact evidence shapes the gate and the claim validator expect.
