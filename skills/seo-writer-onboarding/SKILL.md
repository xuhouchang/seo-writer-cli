---
name: seo-writer-onboarding
description: >-
  Onboard a new brand into seo-writer: record its website, crawl it, run a
  baseline SEO audit, write a feature summary for customer confirmation, and
  configure DataForSEO/Reddit credentials. Use when a new customer/brand is
  being set up for the first time, before any article production.
---

# seo-writer-onboarding — new brand setup

Bring a brand into the seo-writer pipeline in four steps. The CLI provides the
building blocks; you (the agent) do the extraction, the summarising and the
customer-facing questions. Everything the CLI learns is stored locally under
the brand's directory — nothing customer-specific ever ships in the repo.

## CLI shell contract

This Skill is an instruction shell around the local Python CLI; it is not a
second implementation. Use explicit workspace configuration and JSON output:

```bash
export SW="seo-writer --data-dir ~/.seo-writer --workspace default --json"
```

Read successful business results from stdout, errors from stderr, and preserve
the CLI exit-code contract: `0` success, `1` business/validation failure, `2`
parameter or usage error. For tests, point `--data-dir` at a temporary
directory and use only synthetic fixtures/local HTTP stubs. Do not run real
OAuth or provider requests from the Skill test path.

## Workflow

```bash
export SW="seo-writer --data-dir ~/.seo-writer --json"

# 0. brand must exist first
$SW brand create <brand>

# 1. record the customer's website (local memory file)
$SW onboard site <brand> --url https://<customer-website>/   # -> status: draft

# 2. crawl + SEO audit (plain HTTP, no key, no LLM)
$SW onboard fetch <brand>
#    -> crawl/ artifacts under <data-dir>/<workspace>/brands/<brand>/site-crawl/
#       index.html, content.txt (page text), seo-audit.yaml
#    audit: ~100 rules across core/htmlval/social/content/a11y/images/url/
#       mobile/i18n/security/technical/schema/eeat/legal/perf + robots.txt +
#       sitemap.xml — re-implemented from seo-skills/seo-audit-skill (MIT,
#       see NOTICE); score 0-100, rubric field records the upstream source

# 3. YOU read content.txt, extract the product's features, write site.md
#    (same dir as above), then the CUSTOMER reviews and confirms it
$SW onboard confirm <brand> --approver "<customer-email>"    # -> status: confirmed

# 4. provider credentials (account-level): DataForSEO + Reddit
$SW providers configure                                     # interactive prompts
$SW providers status                                        # configured/verified?
```

## Step 3 — feature extraction (your job)

Read `<data-dir>/<workspace>/brands/<brand>/site-crawl/content.txt` (and
`index.html` when more context is needed) and write `site.md`:

```markdown
## <Product name> — feature summary (draft)

### What the product does
- <1-2 sentences, from the customer's own homepage wording>

### Concrete features (evidence-backed)
- <feature 1 — only what the page text supports>
- <feature 2 — only what the page text supports>

### Claims to avoid (not supported by the page)
- <anything the homepage suggests but does not actually state>

### Open questions for the customer
- <gaps to confirm with the customer before drafting facts.yaml>
```

Rules:

- **Quote, do not invent.** Every bullet must trace to page text. If the
  homepage says "batch processing", write "batch processing" — not "scales to
  enterprise fleets".
- **Flag unsupported claims.** Superlatives and guarantees that appear on the
  page (or are common in the industry) but are not backed by stated features
  belong in "Claims to avoid" — the facts ledger later decides their fate.
- **Ask before guessing.** Missing target audience, pricing model, or
  "not-a-fit" boundaries go to "Open questions for the customer".
- After the customer edits/reviews, `onboard confirm` stamps who approved and
  when. The confirmed summary is the seed for `facts.yaml` (see the
  seo-writer production skill) — reuse its wording verbatim.

## Step 4 — provider credentials

- `providers configure` prompts for DataForSEO (login/password) and Reddit
  (client_id/client_secret). Secrets are written to a chmod-600
  `.secrets.yaml` in the data dir — never to git, never echoed in logs.
- Each provider is verified live on configure (DataForSEO ping / Reddit OAuth
  token); failures are stored as `last_error` and exit non-zero.
- Existing environment variables (DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD,
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET) are offered as defaults — press
  enter to use them. Reddit requests honour `REDDIT_PROXY_URL`.
- Configured-but-unverified credentials still block nothing today: the real
  search/community providers are a Phase 2 swap behind the same interfaces.

## Step 5 — connect Google Search Console (GSC)

Performance data is pulled from the customer's GSC property into the local
data dir (zero third-party runtime deps, stdlib only — nothing leaves the
customer machine). Three paths, A → B → C:

1. **`gsc setup <brand>`** — shows credential state and which path to take.
   - **Path A (preferred)** — gcloud ADC already on the machine:
     `gcloud auth application-default login` then `gsc auth <brand> --gcloud`.
   - **Path B** — customer's own OAuth client: they download
     `client_secret_*.json` from their Google Cloud console
     (Desktop app type; enable the Search Console API). `gsc setup` imports
     it (chmod-600, copied into the data dir), then `gsc auth <brand>` opens
     the PKCE consent flow (or `--no-launch-browser` to paste the URL and
     code manually).
   - **Path C** — no credentials at all: the customer exports
     "Search results" CSV from the GSC UI, `gsc import <brand> <file.csv>`.
2. **`gsc sites <brand>`** — list properties the account can access.
3. **`gsc connect <brand> --property <url>`** — bind the property to the
   brand (e.g. `sc-domain:example.com` or `https://example.com/`).
4. **`gsc pull <brand>`** — Search Analytics, incremental: repeated runs
   skip synced dates and make **zero** API calls (`--force` to re-pull).
   Handles 429/5xx with exponential backoff and the 1,200 QPM limit.
5. **`gsc insights <brand>`** — high-impressions/low-CTR queries, rising
   queries, and URL performance against the `onboard fetch` audit baseline
   — feeds the customer-facing report.
6. Optional extras: `gsc inspect --brand <brand> --url <url>` (indexing
   status, uses the bound property), `gsc sitemap submit` (submit/refresh
   sitemaps).

Credentials (refresh token, client json) live only under
`<data-dir>/.../gsc/<brand>/`, chmod-600; `gsc status` shows binding and sync
ranges without printing any secret. If the customer has no credentials at
all, path C still gives insights from their UI export.

## Failure handling

| symptom | cause | fix |
|---|---|---|
| `onboard site` exit 2 | invalid URL (scheme/host) | pass `https://host/`; brand must exist |
| `onboard fetch` exit 1 | site unreachable (DNS/refused/timeout) | check the URL, retry; network is the customer's |
| `onboard fetch` exit 1 | site recorded but never fetched | run fetch again after fixing the URL |
| audit `error` checks | reachability / title / h1 / https / canonical conflicts | report to the customer as SEO findings, don't paper over |
| `onboard confirm` exit 2 | no `site.md` yet, or empty | you must write the draft first |
| `providers configure` exit 2 | missing field (all required) | supply every field; empty = env default |
| `providers verify` exit 1 | credentials rejected by the API | tell the customer to regenerate keys (DataForSEO: API password ≠ account password) |

## After onboarding

The confirmed feature summary feeds the production pipeline:

1. Draft `facts.yaml` from the confirmed summary (allowed_wording verbatim,
   blocked claims from "Claims to avoid" as `safety_level: blocked`).
2. `seo-writer brand facts import <brand> <facts.yaml>` + policy import.
3. Switch to the **seo-writer** skill (article production, research gate,
   approval, claim-safe drafting) — that skill's gate counts are
   provider-driven, which is where Phase 2 search/community providers
   (DataForSEO, Reddit) plug in.
