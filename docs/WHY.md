# Why SEO Writer exists

## The problem: AI makes content fast, and empty

Most AI-generated SEO articles share one recognizable flaw: they are smooth,
well-structured, and say nothing. The model averages the top ten results — that
is literally what it was trained to do — and the article agrees with everyone
because it takes no position. It reads like it was written by a committee that
was paid to be harmless.

That shows up in five concrete ways:

1. **No point of view.** No opinion, no stance, no "here is what actually
   worked for our customers." Search engines and readers both punish this. An
   article that anyone could have written is treated like something no one
   needed.
2. **Claims that cannot be verified.** The model predicts words that *sound*
   confident: "industry-leading," "the fastest," "operators report X." None of
   it traces to a source. In fintech, health, or B2B SaaS, an unsupported claim
   is not a quality problem — it is a compliance incident.
3. **No research discipline.** The brief is a title and a keyword. The article
   is written before anyone opened a single competing page or community thread.
   The "research" is the model's training data — stale, and wrong as often as
   it is right.
4. **No human checkpoint.** Outline goes straight to draft, draft goes straight
   to publish. Nobody approves the plan. Nobody catches the sentence claiming
   the product does something it does not. The process has no brakes.
5. **Data and writing never talk.** The SEO data (Search Console, site audits,
   content gaps) lives in one tool and the article lives in another. Nobody
   connects what the data says to what the article says.

## The answer: opinions, verified

SEO Writer is a local-first tool built on one trade: instead of adding another
model that writes faster, it puts a governance pipeline in front of the
writing. The writing can still be fast — but what ships is *true, on-point,
and worth reading*. Three mechanisms do the heavy lifting.

### A research gate that cannot be skipped

The outline is never generated before the research passes a floor: enough
queries, enough opened SERP pages, enough community threads, across enough
subreddits. The gate is enforced by code, not by a "please do better research"
reminder that a model will ignore. If the evidence is not there, the pipeline
stops and tells you exactly what is missing.

### A claims ledger that blocks fiction

Every brand gets a facts file: what the product actually does, in wording the
company approves. A validator scans the entire article — draft, FAQ, metadata —
and rejects any substantive claim that is not traceable to an approved entry.
A claim that is not in the ledger cannot ship, and blocked claims are flagged
with the exact offending sentence.

### A human approval checkpoint

The outline is produced and then *stops*. Nothing gets drafted until a person
explicitly approves that exact revision. If the facts change, the policy
changes, or the outline is regenerated, the approval is automatically
invalidated and the run is demoted — no silent drift, no stale approvals.

## The part people underestimate: hand-holding

The bundled Skill tells the agent exactly what a good SEO article needs —
search intent, audience/pain/differentiator, an editorial angle, information
gain over what already ranks, per-section source maps, title and description
length caps. It is the checklist your best human editor would demand, made
executable. The agent is coached at every step instead of being left alone
with a blank prompt.

## What you can do with it

- **Write articles that clear a quality bar, every time.** From a one-line
  topic brief to a validated, claim-safe, exported article — with an audit
  trail the whole way.
- **Use the AI agent you already trust.** You, Coze, Codex, Claude Code, or
  any agent write the outline, draft, and metadata as files; the CLI enforces
  the governance. No LLM API key required.
- **Onboard a new brand properly.** Crawl the site, run a 0–100 SEO audit,
  extract an evidence-backed product brief, and confirm it with the customer —
  before the first article is written.
- **Do the SEO analysis where the writing happens.** Google Search Console
  setup, pull, and insights (queries, positions, CTR), plus content-gap
  analysis against the current run's evidence. The data lands next to the
  writing, not in a separate tool you never open.

## Who this is for

- **Independent SEOs and bloggers** who want AI speed without AI-slop quality.
- **Content agencies** that need client-verifiable claims and an approval
  trail for every published post.
- **AI power users** running Coze, Codex, Claude Code, or local agents who want a
  governed writing pipeline instead of "just ask the model."
- **Teams under compliance pressure** who cannot afford one unverifiable claim
  in a published article.

It is a CLI plus a Skill. No SaaS, no monthly fee, no data leaving your
machine. Credentials, facts, and drafts stay in your data directory.

## The pipeline in one picture

```
topic brief → research → GATE → content map & outline → HUMAN APPROVAL
           → draft → metadata → VALIDATION → export (article + manifest)
```

Every arrow is a command. Every command records an audit event. Nothing
happens in the background silently.

## Getting started

The README quick start walks a full article end-to-end in about a minute,
fully offline, with synthetic example data. For the technical design, see
`docs/ARCHITECTURE.md`.