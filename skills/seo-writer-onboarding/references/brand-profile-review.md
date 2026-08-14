# Brand profile factual review

Use this reference after the website evidence brief is ready.

The review contains only these customer-confirmable fields:

- Company and website
- Target audience
- Primary use case
- Features
- Advantages
- Benefits
- Limitations and non-capabilities
- Competitor candidates and alternatives
- Primary market
- Content language (English)

Generate the local review:

```bash
sw onboard brand-profile <brand>
```

The command returns paths to the canonical JSON, JSON Schema, and
`brand-profile-review.html`. The customer opens the HTML, edits factual fields,
and selects **Download review JSON**. Import the downloaded file:

```bash
sw onboard import-brand-profile <brand> <brand-profile-review.json>
```

The CLI rejects a stale revision, stale input hash, wrong workspace, or wrong
brand. A successful import stores the original review under
`brand-profile-imports/`, creates a new canonical revision, and reports factual
completeness. It does not add claims to `facts.yaml` automatically.

Do not add questions about customer stories, failures, practitioner lessons,
contrarian opinions, unique points of view, or proof. Those questions belong
only beside a concrete section or Viewpoint Card in the outline review.
