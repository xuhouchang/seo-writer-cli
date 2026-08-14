---
name: seo-writer-onboarding
description: >-
  Onboard a new brand into seo-writer: record its website, crawl it, run a
  baseline SEO audit, write an evidence-backed product brief with FAB, target
  audience, limitations, and competitor context for customer confirmation,
  and configure and verify DataForSEO/Reddit credentials. Use when a new
  customer/brand is being set up for the first time, before article production.
---

# seo-writer-onboarding — new brand setup

Bring a brand into the seo-writer pipeline in four steps. The CLI provides the
building blocks; you (the agent) do the extraction, the summarising and the
customer-facing questions. Everything the CLI learns is stored locally under
the brand's directory — nothing customer-specific ever ships in the repo.

This is an English-first onboarding Skill for overseas customers. Every
customer-facing heading, field label, helper message, question, validation
message, error, empty state and generated onboarding artifact must be in
English. Preserve customer-provided names, URLs and evidence verbatim; do not
translate or rewrite them merely to enforce the English UI.

Keep onboarding factual. Ask only for information a customer can directly
confirm here: target audience, use case, FAB, limitations and non-capabilities,
competitor candidates, market, website and language. Do not ask for customer
stories, failure recollections, practitioner lessons, contrarian opinions,
unique points of view or proof requests during onboarding. Defer those
behavioural prompts until the production workflow has generated a content map
and an outline/framework; then ask them beside the relevant section or
Viewpoint Card.

## CLI shell contract

This Skill is an instruction shell around the local Python CLI; it is not a
second implementation. Use explicit workspace configuration and JSON output:

```bash
export SEO_WRITER_HOME="/absolute/path/to/seo-writer"
sw() {
  "$SEO_WRITER_HOME/bin/seo-writer" \
    --data-dir ~/.seo-writer --workspace default --json "$@"
}
```

Read successful business results from stdout, errors from stderr, and preserve
the CLI exit-code contract: `0` success, `1` business/validation failure, `2`
parameter or usage error. For tests, point `--data-dir` at a temporary
directory and use only synthetic fixtures/local HTTP stubs. Do not run real
OAuth or provider requests from the Skill test path.

## Workflow

```bash
# 0. brand must exist first
sw brand create <brand>

# 1. record the customer's website (local memory file)
sw onboard site <brand> --url https://<customer-website>/   # -> status: draft

# 2. crawl + SEO audit (plain HTTP, no key, no LLM)
sw onboard fetch <brand>
#    -> crawl/ artifacts under <data-dir>/<workspace>/brands/<brand>/site-crawl/
#       index.html, content.txt (page text), seo-audit.yaml
#    audit: ~100 rules across core/htmlval/social/content/a11y/images/url/
#       mobile/i18n/security/technical/schema/eeat/legal/perf + robots.txt +
#       sitemap.xml — re-implemented from seo-skills/seo-audit-skill (MIT,
#       see NOTICE); score 0-100, rubric field records the upstream source

# 3. YOU read content.txt, extract the product evidence, write site.md
#    (same dir as above), then the CUSTOMER reviews and confirms it
sw onboard confirm <brand> --approver "<customer-email>"    # -> status: confirmed

# 3b. generate the English factual browser review, then import its download
sw onboard brand-profile <brand>
sw onboard import-brand-profile <brand> <brand-profile-review.json>

# 4. provider credentials (account-level): DataForSEO + Reddit
sw providers configure --name dataforseo                   # user supplies login/password
sw providers configure --name reddit                       # user supplies client id/secret
sw providers status                                        # both must be configured/verified
```

## Step 3 — product evidence extraction (your job)

Read `<data-dir>/<workspace>/brands/<brand>/site-crawl/content.txt` (and
`index.html` when more context is needed) and write `site.md`:

```markdown
## <Product name> — product evidence brief (draft)

### What the product does
- <1-2 sentences, from the customer's own homepage wording>

### Target audience
- <who it is for and the relevant job/use case, or "not stated; customer confirmation required">

### Feature
- <what the product can do, using only supported wording>

### Advantage
- <why the feature is preferable or differentiated; cite supporting customer/competitor evidence, or mark unknown>

### Benefit
- <the outcome for the target audience; label an inference and request confirmation when not directly stated>

### Limitations and non-capabilities
- <what it cannot do, explicit exclusions, prerequisites, or "not stated; customer confirmation required">

### Competitor context
- <named/category competitor, comparison scope, source/date, or "not stated; customer confirmation required">

### Claims to avoid
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
- **Ask factual questions only.** An onboarding open question must request a
  fact the customer can directly confirm. Do not use this section to ask for
  cases, failures, practitioner judgment or unique opinions; the production
  Skill asks those later against a concrete outline section.
- **Keep FAB distinct.** Feature is a capability, Advantage is a supported
  differentiation, and Benefit is the audience outcome. Do not restate the
  same marketing sentence three times.
- **Describe boundaries, not vague weaknesses.** Record explicit unsupported
  functions, exclusions, prerequisites, and not-a-fit scenarios. Never infer a
  defect from missing website copy.
- **Treat competitors as evidence.** Record who/which category, what is being
  compared, and the source/date. Do not invent rankings or superiority claims.
- `onboard confirm` rejects missing or empty required sections. When evidence
  is absent, state that explicitly and put the question to the customer.
- After the customer edits/reviews, `onboard confirm` stamps who approved and
  when. The confirmed brief is the seed for `facts.yaml` (see the
  seo-writer production skill) — reuse its wording verbatim.

The browser review fields and local import loop are defined in
`references/brand-profile-review.md`. JSON remains canonical; HTML is a
deterministic review surface and never becomes the database.

## Step 4 — provider credentials

- `providers configure` prompts for DataForSEO (login/password) and Reddit
  (client_id/client_secret). Secrets are written to a chmod-600
  `.secrets.yaml` in the data dir — never to git, never echoed in logs.
- Each provider is verified live on configure (DataForSEO ping / Reddit OAuth
  token); failures are stored as `last_error` and exit non-zero.
- Existing environment variables (DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD,
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET) are offered as defaults — press
  enter to use them. Reddit requests honour `REDDIT_PROXY_URL`.
- Production research requires a policy selecting `dataforseo`, `reddit` and
  `http`. Missing or unverified credentials are a hard onboarding blocker;
  research never silently falls back to mock data.

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
   status, uses the bound property). SEO Writer is read-only and does not
   submit or modify Sitemaps.

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
| `onboard confirm` exit 2 | no/empty `site.md`, or a required product-input section is missing/empty | complete the evidence brief; use an explicit unknown plus customer question when evidence is absent |
| `import-brand-profile` exit 1 | wrong brand/workspace, revision, or input hash | regenerate the current HTML and import its newly downloaded JSON |
| `providers configure` exit 2 | missing field (all required) | supply every field; empty = env default |
| `providers verify` exit 1 | credentials rejected by the API | tell the customer to regenerate keys (DataForSEO: API password ≠ account password) |

## After onboarding

The confirmed product evidence brief feeds the production pipeline:

1. Draft `facts.yaml` from the confirmed brief (allowed_wording verbatim,
   blocked claims from "Claims to avoid" as `safety_level: blocked`).
2. `seo-writer brand facts import <brand> <facts.yaml>` + policy import.
3. Switch to the **seo-writer** skill (article production, research gate,
   approval, claim-safe drafting) — that skill's gate counts are
   provider-driven. Before switching to the production skill, import the real
   provider policy template, replace its brand slug, and confirm `providers
   status` shows both external providers verified.
