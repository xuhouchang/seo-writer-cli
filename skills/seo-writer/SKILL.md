---
name: seo-writer
description: >-
  Run the seo-writer CLI pipeline: research-gated, approval-governed,
  claim-safe SEO article production. Use when producing SEO content that must
  be traceable to approved brand facts and pass a strict gate.
---

# seo-writer — Agent pipeline

The agent authors the content; the CLI enforces the governance. You write the
outline, the draft and the metadata as files, and hand them to the CLI via
`--from-file` — **no LLM API configuration is needed**; the CLI records,
approves, validates and exports your work.

## Principles (non-negotiable)

1. **Never skip the gate.** The run must pass `validate-research` before any
   outline exists. A failed gate means the research evidence is insufficient
   — extend the research, do not hand-wave.
2. **Never draft without approval.** `run draft` / `run metadata` refuse
   until an outline revision is explicitly approved. Do not try to bypass
   this; re-approve instead.
3. **Only approved claims reach the copy.** Quote the brand facts ledger's
   `allowed_wording` verbatim for substantive product claims. Never use
   blocked claims, and never use generic unsafe patterns: superlatives
   ("world's first", "best"), guarantees ("guaranteed", "always saves"),
   no-review language ("no human review").
4. **Read every exit code.** Business failures exit 1 with a JSON reason on
   stderr (`--json`). Fix the reported reasons — never ignore a validation
   failure.
5. **Idempotency is your friend.** Re-running the same command with the same
   file short-circuits. Editing the file and re-importing creates a new
   outline revision and invalidates the previous approval — re-approve.

## CLI shell contract

This Skill is the product shell; the Python CLI is the execution runtime. Use an
explicit data directory and request machine-readable output:

```bash
export SEO_WRITER_HOME="/absolute/path/to/seo-writer"
sw() {
  "$SEO_WRITER_HOME/bin/seo-writer" \
    --data-dir ~/.seo-writer --workspace default --json "$@"
}
```

On success, consume JSON from stdout. On failure, inspect JSON on stderr and
honour the exit code: `0` success, `1` business/validation failure, `2`
parameter or usage error. Do not treat a non-zero exit code as a successful
step, and do not duplicate the CLI's business logic in the Skill.

For tests or demos, always replace `~/.seo-writer` with a temporary data
directory. Never use real customer data, OAuth, provider credentials, paid
requests, or model weights in fixtures.

## Prerequisites

```bash
# one-time per machine: install + init + import brand facts/policy
sw init
sw brand create <brand>
sw project create <brand> <project>
sw brand facts import <brand> <path/to/facts.yaml>
sw brand policy import <brand> <path/to/policy.yaml>

# production research prerequisite: the user must configure these manually
sw providers configure --name dataforseo
sw providers configure --name reddit
sw providers status

Use a policy selecting `dataforseo` for keyword/SERP, `reddit` for community,
and `http` for opened pages. Mock policies are for tests and synthetic demos
only. If a production provider is missing or unverified, stop and ask the user
to complete onboarding; never substitute mock evidence.
```

The brand facts ledger (`facts.yaml`) is the single source of truth for what
may be claimed. Read it (`brand facts show`) before writing any copy.

## Standard workflow

```bash
# 1. create the run from a topic brief
sw run create <brand> <project> --brief <topic.yaml>          # -> run_id

# 2. research + gate
sw run research <run_id>                                      # evidence_rows: N
sw run validate-research <run_id>                             # gate; exit 1 if gaps

# 2b. YOU create an evidence-backed content map, then import and render it
sw run gap-map <run_id> --from-file content-map.json
sw run render <run_id> --view content-map
sw run render <run_id> --view opportunities
# customer edits only priority/exclusion decisions and downloads the JSON
sw run import-opportunity-review <run_id> <opportunity-review.json>  # -> new opportunity revision

# 3. YOU write the outline (structure requirements below) and import it
#    (same file re-imported = idempotent; edited file = new revision)
sw run outline <run_id> --from-file outline.md                # -> outline_revision: 1

# 3b. render the section-bound viewpoint review; import the downloaded JSON
sw run render <run_id> --view outline
sw run import-review <run_id> <outline-review.json>           # -> new revision

# 4. explicit human approval (the approver is audited)
sw run approve <run_id> --revision 1 --approver "editor@example.com"

# 5. YOU write the draft (claim rules below) and import it
sw run draft <run_id> --from-file draft.md

# 6. YOU write the metadata (length caps below) and import it
sw run metadata <run_id> --from-file metadata.yaml

# 7. full validation — claims, structure, lengths, approval
sw run validate <run_id>                                      # -> completed

# 8. export: article.md + manifest.json (full traceability)
sw run export <run_id> --format markdown [--out-dir ./out]
sw run export <run_id> --format html [--out-dir ./out]
```

## Content map and outline review

The content map uses only current-run evidence ids and classifies gaps as
`topic`, `intent`, `format`, or `depth`. A blank competitor area is not an
opportunity unless buyer evidence and sampled competitor evidence both exist.
Coverage uses the deterministic 0-3 rubric. Opportunity cards keep Market Gap
Confidence, Brand Fit, and Differentiation Readiness separate; do not invent a
combined demand score.

Opportunity review is a typed, fail-closed write-back step. Import only the
JSON downloaded from the latest rendered page. The CLI checks workspace,
brand, project, article, run, opportunity revision, content-map hash, artifact
hash, manifest hash, decision fields, and opportunity ids. A successful import
creates a new opportunity revision and audit event; it never overwrites
`content-map.json` or changes evidence, Market Gap Confidence, Brand Fit, or
Differentiation Readiness. Treat review notes as customer-visible and never put
confidential information in them. Re-render after import before collecting
more feedback.

Behavioural questions are allowed only after an outline/framework exists.
Every contextual prompt must carry a `section_id` or `viewpoint_id`, and each
Viewpoint Card has at most two high-value prompts. Review choices are Confirm,
Confirm with edits, Reject, Ask an internal expert, Add an example, and True,
but confidential. Confidential input never enters customer-facing HTML or the
final article export.

## Content requirements (what the CLI enforces)

### Outline (`run outline --from-file`)

Must contain these section markers (structure is validated, exit 1 on
failure):

```markdown
## Article Title: <title>
### Target Keywords:
### Search Intent: <INTENT>
### Section 1: <section title>
```

See `skills/seo-writer/examples/outline-template.md` for the full template
(Audience/Pain/Differentiator, editorial angle, AIO/SERP consensus,
information-gain plan, per-section key points + source map).

### Draft (`run draft --from-file`)

- Substantive product claims use the exact `allowed_wording` from the
  approved facts ledger (claim-safe validator checks the full corpus —
  draft, FAQ and metadata together).
- Blocked claims (`safety_level: blocked`) and their `disallowed_wording`
  must never appear — the validator reports the offending sentence.
- Attribute qualified claims to their source ("operators report …") exactly
  as the ledger's `guardrail` says.

### Metadata (`run metadata --from-file`, YAML)

```yaml
meta_title: "…"              # ≤ 60 chars
meta_description: "…"        # ≤ 155 chars
slug: my-post-slug           # lowercase, dashes
faq:                         # feeds the FAQPage schema
  - q: "…"
    a: "…"
image_alt_texts: ["…"]       # each ≤ 125 chars
```

## Failure handling

| symptom | cause | fix |
|---|---|---|
| `validate-research` exit 1 | gate gaps (queries/SERP/threads/subreddits/second platform) | run research again / add evidence; re-run gate |
| `outline --from-file` exit 1 | structure markers missing or empty file | add the required `##`/`###` sections |
| `gap-map` exit 1 | malformed map or unknown evidence id | use only evidence from `run evidence`; fix buyer and competitor refs |
| `import-opportunity-review` exit 1 | stale/mismatched envelope, tampered hash, unknown field or opportunity | render the latest opportunity page and import a fresh, unmodified download |
| `import-review` exit 1 | stale revision/input hash or unknown viewpoint | render the latest outline and import a fresh download |
| `draft` / `metadata` exit 1 | no approval or approval invalidated (facts/policy changed) | `run approve` again; re-check `run status` |
| `validate` exit 1 | claim-safety / length failures | fix the named sentences in draft.md / metadata.yaml, re-import with the **same** or **new** file as appropriate, re-validate |
| `run.blocked` | provider failure or validation failure | `run retry --step <step>` after fixing the cause |

Check `run status` and `run evidence --json` to diagnose. The exported
`manifest.json` is the audit record: rules version, facts hash, outline
revision + hash, approval, evidence ids, costs, event log.
