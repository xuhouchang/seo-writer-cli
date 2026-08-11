# SEO Writer v0.1.0 public-beta release

SEO Writer is distributed as an Agent Skill plus a local Python CLI runtime.
It is not published to PyPI and it is not a hosted SaaS.

## Release scope

The public beta includes:

- `skills/seo-writer`: article workflow, approval and claim-safety shell;
- `skills/seo-writer-onboarding`: customer-site onboarding and GSC workflow;
- the local `seo-writer` CLI, SQLite workspace and export artifacts;
- synthetic examples, offline tests and a local release smoke test;
- user-configured production adapters for DataForSEO keyword/SERP research,
  Reddit/community evidence and HTTP page extraction.

Bundled mock providers are test/demo-only and must not be presented as live
market research. In production, the customer must configure and verify
DataForSEO and Reddit, then import a policy selecting `dataforseo`, `reddit`
and `http`. The Skill authors outline/draft/metadata through `--from-file`, so
this release does not require a direct LLM API key. GSC becomes real only after
the customer explicitly configures OAuth, or imports a dated GSC CSV.

## Build and verify the source bundle

From a clean checkout:

```bash
uv sync --frozen
uv run python scripts/build_release_bundle.py
shasum -a 256 -c dist/seo-writer-0.1.0-source.tar.gz.sha256
```

The bundle is curated: it contains source, lock file, Skill instructions,
examples, documentation, launcher, `LICENSE`, `NOTICE` and a release manifest.
It excludes databases, CSV files, credentials, environment files, customer
data and model weights.

## Install and run on macOS or Linux

Requirements: `uv` and a POSIX shell. Python is resolved and managed by uv from
the locked project; do not use `pip install`.

```bash
tar -xzf seo-writer-0.1.0-source.tar.gz
cd seo-writer-0.1.0
uv sync --frozen
./bin/seo-writer --version
uv run python scripts/release_smoke.py --launcher ./bin/seo-writer
```

Register `skills/seo-writer/` and/or `skills/seo-writer-onboarding/` with the
target Agent host according to that host's Skill directory convention. Set
`SEO_WRITER_HOME` to the extracted application directory; the Skills call the
explicit launcher and do not depend on an arbitrary `seo-writer` on `PATH`.

## Data and workspace

The default data root is `~/.seo-writer`; each workspace is a child directory.
For customer delivery, pass explicit paths:

```bash
./bin/seo-writer --data-dir /path/to/customer-data --workspace acme --json init
```

Successful business results are JSON on stdout. Business failures exit `1` and
usage failures exit `2`; with `--json`, both write structured errors to stderr.
Human help/version output remains text.

Never place a database, GSC CSV, OAuth token, provider secret or customer facts
inside the application directory. Model weights are not required by this
release; if added later, they must live under `~/.cache/models/`.

Configure production providers after installation:

```bash
./bin/seo-writer --data-dir ~/.seo-writer --json providers configure --name dataforseo
./bin/seo-writer --data-dir ~/.seo-writer --json providers configure --name reddit
./bin/seo-writer --data-dir ~/.seo-writer --json providers status
```

The configure commands perform the user's explicit live verification. They
must not be run in automated tests; tests use local HTTP stubs.

## Container operation

Use the same source bundle in one image, run `uv sync --frozen` during the image
build, and mount the data directory as a volume. Inject credentials only at
runtime. Do not bake workspaces, tokens, CSV files or model assets into an
image. This release intentionally does not claim a prebuilt container image.

## Upgrade, rollback and uninstall

Before upgrading, stop active jobs and back up the data directory. Extract the
new release beside the old one, run `uv sync --frozen` and the offline smoke,
then point the Skill host at the new `SEO_WRITER_HOME`. Keep the old application
directory until the new version has opened a copy of the workspace successfully.

Rollback by pointing the Skill host back to the previous application directory
and restoring the pre-upgrade data backup if a schema migration occurred.

Uninstall by removing the application directory. Customer data is deliberately
separate and is not removed automatically; delete it only with explicit customer
authorization.

## Final release gate

```bash
uv sync --frozen
uv run ruff check .
uv run pytest tests/ -q
uv run python -m compileall -q src
uv lock --check
uv build
uv run python scripts/release_smoke.py --launcher ./bin/seo-writer
uv run python scripts/build_release_bundle.py
gitleaks detect --no-git --source . --no-banner --redact --exit-code 1
```

CI must pass on Python 3.12 and 3.13 before publishing a Git tag or GitHub
Release. A local pass is release-candidate evidence, not proof that remote CI
or a public release has happened.
