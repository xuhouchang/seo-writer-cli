"""run command group: ArticleRun lifecycle."""

from __future__ import annotations

from typing import Annotated

import typer

from .. import services
from . import run_app
from ._common import _guard, _open, _require_both, _run_context, state


@run_app.command("create")
def run_create(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
    project_slug: Annotated[str, typer.Argument(help="Project slug")],
    brief: Annotated[str, typer.Option("--brief", "-b", help="topic.yaml path")],
) -> None:
    """Create an ArticleRun: snapshots brief, facts hash and policy hash."""

    def run() -> dict:
        ws, db = _open()
        return services.create_run(ws, db, brand_slug, project_slug, brief)

    return _guard(run)


@run_app.command("list")
def run_list(
    brand_slug: Annotated[str | None, typer.Option(help="Filter by brand")] = None,
    project_slug: Annotated[str | None, typer.Option(help="Filter by project")] = None,
) -> None:
    """List runs, optionally filtered by brand/project."""

    def run() -> dict:
        _require_both(brand_slug, project_slug)
        _, db = _open()
        project_id = None
        if brand_slug and project_slug:
            project_id = services.resolve_project(db, brand_slug, project_slug)[1]["id"]
        rows = db.list_runs(project_id)
        return {
            "runs": [
                {
                    "run_id": r["id"],
                    "project_id": r["project_id"],
                    "status": r["status"],
                    "step": r["step"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    return _guard(run)


@run_app.command("research")
def run_research(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:research)")
    ] = None,
) -> None:
    """Execute the research step: keyword, SERP, webfetch, community providers."""

    def run() -> dict:
        _, db = _open()
        run, _brand, policy = _run_context(db, run_id)
        return services.run_research(
            db,
            run,
            policy,
            key=idempotency_key,
            provider_data_dir=state.data_dir,
        )

    return _guard(run)


@run_app.command("validate-research")
def run_validate_research(
    run_id: Annotated[str, typer.Argument(help="Run id")],
) -> None:
    """Run the research gate. Failure blocks the run (exit 1, audited)."""

    def run() -> dict:
        _, db = _open()
        run, _brand, policy = _run_context(db, run_id)
        return services.validate_research(db, run, policy)

    return _guard(run)


@run_app.command("outline")
def run_outline(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    from_file: Annotated[
        str | None,
        typer.Option(
            "--from-file",
            help="Import outline markdown produced externally (no LLM call); audit is marked origin=external",
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:outline)")
    ] = None,
) -> None:
    """Generate the outline (LLM provider) or import one (--from-file); the gate must have passed."""

    def run() -> dict:
        _, db = _open()
        run, brand, policy = _run_context(db, run_id)
        return services.run_outline(
            db,
            run,
            brand,
            policy,
            key=idempotency_key,
            from_file=from_file,
            provider_data_dir=state.data_dir,
        )

    return _guard(run)


@run_app.command("gap-map")
def run_gap_map(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    from_file: Annotated[str, typer.Option("--from-file", help="content-map.json path")],
) -> None:
    """Import and validate a current-run evidence-backed content map."""

    def run() -> dict:
        ws, db = _open()
        article_run, _brand, _policy = _run_context(db, run_id)
        return services.import_gap_map(ws, db, article_run, from_file)

    return _guard(run)


@run_app.command("render")
def run_render(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    view: Annotated[str, typer.Option("--view", help="content-map | opportunities | outline")],
) -> None:
    """Deterministically render a read-only self-contained HTML review."""

    def run() -> dict:
        ws, db = _open()
        article_run, brand, _policy = _run_context(db, run_id)
        return services.render_run_view(ws, db, article_run, brand, view)

    return _guard(run)


@run_app.command("import-review")
def run_import_review(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    review_json: Annotated[str, typer.Argument(help="Downloaded outline-review.json path")],
) -> None:
    """Import outline feedback and create a new, unapproved outline revision."""

    def run() -> dict:
        ws, db = _open()
        article_run, brand, _policy = _run_context(db, run_id)
        return services.import_outline_review(ws, db, article_run, brand, review_json)

    return _guard(run)


@run_app.command("import-opportunity-review")
def run_import_opportunity_review(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    review_json: Annotated[str, typer.Argument(help="Downloaded opportunity-review.json path")],
) -> None:
    """Import typed opportunity decisions and create a new opportunity revision."""

    def run() -> dict:
        ws, db = _open()
        article_run, brand, _policy = _run_context(db, run_id)
        return services.import_opportunity_review(ws, db, article_run, brand, review_json)

    return _guard(run)


@run_app.command("approve")
def run_approve(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    revision: Annotated[
        int | None, typer.Option("--revision", "-r", help="Outline revision (default: latest)")
    ] = None,
    approver: Annotated[str, typer.Option("--approver", help="Human approver identity")] = "human",
) -> None:
    """Explicitly approve an outline revision (facts hash is bound to it)."""

    def run() -> dict:
        _, db = _open()
        run, brand, _policy = _run_context(db, run_id)
        return services.approve_outline(db, run, brand, revision or run["outline_revision"], approver)

    return _guard(run)


@run_app.command("draft")
def run_draft(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    from_file: Annotated[
        str | None,
        typer.Option(
            "--from-file",
            help="Import draft markdown produced externally (no LLM call); audit is marked origin=external",
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:draft)")
    ] = None,
) -> None:
    """Generate the draft (LLM provider) or import one (--from-file); requires a current approval."""

    def run() -> dict:
        _, db = _open()
        run, brand, policy = _run_context(db, run_id)
        return services.run_draft(
            db,
            run,
            brand,
            policy,
            key=idempotency_key,
            from_file=from_file,
            provider_data_dir=state.data_dir,
        )

    return _guard(run)


@run_app.command("metadata")
def run_metadata(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    from_file: Annotated[
        str | None,
        typer.Option(
            "--from-file",
            help="Import a YAML metadata document produced externally (no LLM call); "
            "audit is marked origin=external",
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:metadata)")
    ] = None,
) -> None:
    """Generate SEO metadata (LLM provider) or import it (--from-file); length checks block."""

    def run() -> dict:
        _, db = _open()
        run, brand, policy = _run_context(db, run_id)
        return services.run_metadata(
            db,
            run,
            brand,
            policy,
            key=idempotency_key,
            from_file=from_file,
            provider_data_dir=state.data_dir,
        )

    return _guard(run)


@run_app.command("validate")
def run_validate(
    run_id: Annotated[str, typer.Argument(help="Run id")],
) -> None:
    """Full corpus validation: structure, claim safety, metadata, approval."""

    def run() -> dict:
        _, db = _open()
        run, brand, policy = _run_context(db, run_id)
        return services.run_validate(db, run, brand, policy)

    return _guard(run)


@run_app.command("export")
def run_export(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    format: Annotated[str, typer.Option("--format", help="Export format: markdown | html")] = "markdown",
    out_dir: Annotated[str | None, typer.Option(help="Copy article + manifest.json here")] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:export)")
    ] = None,
) -> None:
    """Export a completed run as article.md or article.html plus a traceable manifest."""

    def run() -> dict:
        ws, db = _open()
        run, brand, _policy = _run_context(db, run_id)
        return services.run_export(ws, db, run, brand, format, key=idempotency_key, out_dir=out_dir)

    return _guard(run)


@run_app.command("status")
def run_status(
    run_id: Annotated[str, typer.Argument(help="Run id")],
) -> None:
    """Show run status: state, evidence typing counts, costs, approvals."""

    def run() -> dict:
        _, db = _open()
        return services.run_status(db, services.resolve_run(db, run_id))

    return _guard(run)


@run_app.command("evidence")
def run_evidence(
    run_id: Annotated[str, typer.Argument(help="Run id")],
) -> None:
    """List evidence rows with strict origin/fetch typing."""

    def run() -> dict:
        _, db = _open()
        return services.run_evidence(db, services.resolve_run(db, run_id))

    return _guard(run)


@run_app.command("costs")
def run_costs(
    run_id: Annotated[str, typer.Argument(help="Run id")],
) -> None:
    """Show the cost ledger: total, per provider, per entry."""

    def run() -> dict:
        _, db = _open()
        return services.run_costs(db, services.resolve_run(db, run_id))

    return _guard(run)


@run_app.command("retry")
def run_retry(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    step: Annotated[str, typer.Option(help="Step to re-execute: research | outline | draft")],
) -> None:
    """Explicitly re-execute one step with a fresh idempotency key (never silent)."""

    def run() -> dict:
        _, db = _open()
        run, brand, policy = _run_context(db, run_id)
        return services.run_retry(db, run, brand, step, policy, provider_data_dir=state.data_dir)

    return _guard(run)
