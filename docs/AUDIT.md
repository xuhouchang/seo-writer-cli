# seo-writer — Audit model

Every decision that matters is recorded in the per-run audit ledger
(`audit` table, JSON payloads), so an exported article can be walked back to
the facts, approvals, evidence and costs that produced it — and so a
post-hoc review can prove what was *refused* and why, not just what passed.

## 1. Events written

| event | when | payload |
|---|---|---|
| `run.created` | run created | run id, brief snapshot hash |
| `research.completed` | research step finished | evidence count |
| `research_gate.passed` | gate passed | report counts + rules version |
| `research_gate.failed` | gate failed | full gap list + rules version |
| `retry.research` | explicit research retry | previous status |
| `provider.retry` | a transient provider error was retried | provider, op, attempt |
| `outline.generated` | outline revision created | revision, rules version |
| `outline.approved` | human approval | revision, approver, facts hash |
| `approval.invalidated` | facts/policy change demoted runs | run id, new snapshot hash |
| `retry.outline` / `retry.draft` | explicit step retries | previous status |
| `draft.generated` | draft created | outline revision |
| `metadata.generated` | metadata created | outline revision |
| `validation.passed` | `run validate` passed | rules version |
| `validation.failed` | `run validate` failed | full error list, rules version |
| `run.blocked` | any step blocked the run | step, reason |
| `export.created` | export written | format, rules version |

`rules_version` is a constant (`RULES_VERSION = "2026-08-08.1"`), stamped on
gate, outline, validation and export events so an article can be attributed
to the exact rule set that governed it.

## 2. Non-negotiable properties

1. **Failures are audited.** A blocked gate writes `research_gate.failed`
   with the gap list; a blocked run writes `run.blocked` with step+reason; a
   failed validation writes `validation.failed` with every error. There is
   no silent failure path.
2. **Failures are blocking.** Validation failures raise
   `ValidationFailedError` and set the run to `blocked` — the CLI exits 1.
   Nothing is "downgraded" to a warning that still exports.
3. **Approval invalidation is audited.** When facts or policy are
   re-imported, every affected run gets `approval.invalidated` and demotes
   to `outline_pending_approval`; the re-approval then binds the new
   snapshot hash.
4. **Approvals are never deleted, only superseded.** `superseded_at` keeps
   the history: who approved rev 1, against which facts, and when rev 2
   replaced it.
5. **Retries are audited and billed once.** Each transient retry writes
   `provider.retry`; the operation is billed exactly once per idempotency
   key; failed attempts never enter the cost ledger.
6. **Idempotent re-runs do not duplicate audits.** A default-key re-run
   returns the prior result and adds no events, no costs, no artifacts
   (AC8) — so audit counts are stable.

## 3. Reading the ledger

```bash
# per-run audit trail (JSON lines, newest last)
sqlite3 ~/.seo-writer/default/seo-writer.db \
  "SELECT event_type, created_at, payload FROM audit WHERE run_id='<run-id>' ORDER BY id;"

# costs
sqlite3 ~/.seo-writer/default/seo-writer.db \
  "SELECT provider, operation, amount FROM costs WHERE run_id='<run-id>';"
```

The export manifest embeds the audit events, so the artifact itself carries
its own provenance — no separate database required to verify an article.

## 4. What a reviewer can prove from a manifest

Given `manifest.json` alone:

- which run produced the article and under which rules version;
- the brief and facts snapshot (hash + version) that were in force;
- the exact outline revision approved, by whom, when, and against which
  facts hash;
- every evidence row (id, origin, opened-in-run flag) that fed the gate;
- the total cost billed to the run;
- the full event sequence including `validation.passed` and `export.created`.

## 5. CLI visibility

- `run status` — current status/step, outline revision, approval state.
- `run evidence --json` — evidence rows with strict origin typing.
- `run costs` — total + per-operation costs (mock costs in Phase 1).
- Errors always print the machine-readable reason on stderr with `--json`
  (e.g. `{"error": "ValidationFailedError", "reasons": [...]}`) and map to
  exit code 1 (business) or 2 (usage).
