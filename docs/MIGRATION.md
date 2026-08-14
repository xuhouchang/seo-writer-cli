# seo-writer — real provider configuration

Production research uses user-configured adapters behind `ProviderResult`.
Mocks remain available only for tests and synthetic demos. The first rule is
that **secrets never enter YAML, git, logs or error messages**.

## Current production boundary

- DataForSEO supplies keyword volume/suggestions and Google organic SERP data.
- Reddit supplies community search and thread evidence; Stack Exchange is the
  second-platform search.
- The HTTP adapter opens selected pages and stores extracted evidence, never
  raw HTML bodies.
- The Skill authors outline, draft and metadata with `--from-file`; direct LLM
  API generation is a separate future release.
- Mock providers remain the deterministic compatibility contract for tests.

## User onboarding

```bash
seo-writer --data-dir ~/.seo-writer providers configure --name dataforseo
seo-writer --data-dir ~/.seo-writer providers configure --name reddit
seo-writer --data-dir ~/.seo-writer providers status
```

Copy `examples/brand-packs/policy-real.template.yaml`, replace `brand`, and
import it only after both providers show configured and verified. Production
research must not silently fall back to mock evidence.

## Security rules

1. `policy.yaml` may contain provider names, profiles and non-secret base URLs,
   never API keys. Credentials are stored in the chmod-600
   `<data-dir>/.secrets.yaml` file created by onboarding.
2. Manifests, metadata and audit payloads contain provider names and request
   fingerprints, never tokens, headers or passwords.
3. Provider errors expose provider/operation and safe status text only; they do
   not print credentials or authorization headers.
4. Customer facts, GSC data and CSV files stay in the user-owned data
   directory. They never enter this repository or release bundle.
5. Model weights and provider artifacts, if ever needed, belong under
   `~/.cache/models/`, never in the repository.

## Adapter behavior

- DataForSEO 429/5xx responses are retryable; authentication and validation
  failures are permanent. Requests are fingerprinted without auth headers.
- SERP observations retain `aio_visible`, PAA and related-search evidence, and
  distinguish opened page evidence from snippet-only rows.
- Reddit rows classify promotional content and preserve the second-platform
  outcome needed by the research gate.
- HTTP page extraction has bounded timeouts and records truthful fetch method.

## Verification boundary

All automated tests use synthetic fixtures and a local HTTP server stub. No
real DataForSEO, Reddit, GSC, OAuth, LLM or customer request is part of CI.
The customer may perform a small, explicit live smoke after configuring their
own accounts; that acceptance is separate from the local release gate.

```bash
uv run pytest tests/ -q
uv run ruff check .
uv run python -m compileall -q src
uv lock --check
```
