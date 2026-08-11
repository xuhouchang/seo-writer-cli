"""seo-writer command-line interface.

Exit-code contract (see errors.py):
  0  success
  1  business/validation failure (gate, approval, claim safety, provider)
  2  usage error (bad flags, missing workspace, unknown id)

Global options (before the subcommand):
  --data-dir    override data directory (default ~/.seo-writer)
  --workspace   workspace slug (default "default"; env SEO_WRITER_WORKSPACE)
  --json        machine-readable JSON on stdout; errors as JSON on stderr
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from typer import _click as click

from . import RULES_VERSION, __version__, gsc, onboard, services
from .config import Workspace, ensure_workspace, load_yaml_model, resolve_data_dir, resolve_workspace
from .db import Database
from .errors import ProviderError, SeoWriterError, UsageError, ValidationFailedError
from .facts import import_facts
from .models import FactsYaml, PolicyYaml
from .policy import import_policy

app = typer.Typer(help="Local-first, gate-governed SEO content production CLI.", invoke_without_command=True)
brand_app = typer.Typer(help="Brand management.", no_args_is_help=True)
brand_facts_app = typer.Typer(help="Per-brand fact ledger.", no_args_is_help=True)
brand_policy_app = typer.Typer(help="Per-brand run policy.", no_args_is_help=True)
project_app = typer.Typer(help="Project management.", no_args_is_help=True)
run_app = typer.Typer(help="ArticleRun lifecycle.", no_args_is_help=True)
onboard_app = typer.Typer(help="New-brand onboarding (site memory, crawl, audit, confirmation).",
    no_args_is_help=True,)
providers_app = typer.Typer(help="Provider credential configuration (DataForSEO, Reddit).",
    no_args_is_help=True,)
gsc_app = typer.Typer(help="Google Search Console closed loop (measure → iterate).", no_args_is_help=True)

app.add_typer(brand_app, name="brand")
brand_app.add_typer(brand_facts_app, name="facts")
brand_app.add_typer(brand_policy_app, name="policy")
app.add_typer(project_app, name="project")
app.add_typer(run_app, name="run")
app.add_typer(onboard_app, name="onboard")
app.add_typer(providers_app, name="providers")
app.add_typer(gsc_app, name="gsc")


class Ctx:
    data_dir: Path = resolve_data_dir()
    workspace: str = resolve_workspace()
    json: bool = False


state = Ctx()


@app.callback()
def _main(
    ctx: typer.Context,
    data_dir: Annotated[str | None, typer.Option(help="Data directory (default ~/.seo-writer)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", "-w", help="Workspace slug")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show CLI and rules version")] = False,
) -> None:
    if version:
        print(f"seo-writer {__version__} (rules {RULES_VERSION})")
        raise typer.Exit()
    state.data_dir = resolve_data_dir(data_dir)
    state.workspace = resolve_workspace(workspace)
    state.json = json_out
    if ctx.invoked_subcommand is None and not version:
        typer.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open() -> tuple[Workspace, Database]:
    ws = ensure_workspace(state.data_dir, state.workspace)
    return ws, ws.open_db()


def _guard(fn: Callable[[], Any]) -> Any:
    """Run a command body; map SeoWriterError to the exit-code contract."""
    try:
        result = fn()
    except SeoWriterError as exc:
        if state.json:
            payload: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, ValidationFailedError):
                payload.update({"step": exc.step, "reasons": exc.reasons})
            if isinstance(exc, ProviderError):
                payload.update({"provider": exc.provider, "retryable": exc.retryable})
            if isinstance(exc, gsc.GscError):
                payload.update({"retryable": exc.retryable})
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(code=exc.exit_code) from None
    if result is not None:
        if state.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            _render(result)
    return result


def _render(value: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _render(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _render(item, indent)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{value}")


def _run_context(db: Database, run_id: str) -> tuple[dict, dict, dict]:
    """Resolve run + its brand + the brand policy from a run id."""
    run = services.resolve_run(db, run_id)
    brief = json.loads(run["brief_snapshot"])
    brand = services.resolve_brand(db, brief["brand_slug"])
    policy = services.load_policy(db, brand["id"])
    return run, brand, policy


def _require_both(brand: str | None, project: str | None) -> None:
    if (brand is None) != (project is None):
        raise UsageError("--brand and --project must be given together")


def main() -> int:
    """Run Typer while preserving JSON output for command-line usage errors."""
    try:
        result = app(standalone_mode=False)
    except click.ClickException as exc:
        if "--json" in sys.argv[1:]:
            payload = {"error": "UsageError", "message": exc.format_message()}
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            exc.show()
        return int(exc.exit_code)
    return result if isinstance(result, int) else 0


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create the workspace (database + objects directory) if missing."""
    def run() -> dict[str, str]:
        ws, db = _open()
        db.close()
        return {
            "workspace": str(state.data_dir / state.workspace),
            "rules_version": RULES_VERSION,
            "cli_version": __version__,
        }

    return _guard(run)


# ---------------------------------------------------------------------------
# onboard
# ---------------------------------------------------------------------------

_PROVIDER_ENV_HINTS = {
    "dataforseo.login": "DATAFORSEO_LOGIN",
    "dataforseo.password": "DATAFORSEO_PASSWORD",
    "reddit.client_id": "REDDIT_CLIENT_ID",
    "reddit.client_secret": "REDDIT_CLIENT_SECRET",
}


def _prompt_provider_values(prov: str) -> dict[str, str]:
    if state.json:
        raise UsageError("interactive setup needs a terminal; run without --json or set provider env vars")
    values: dict[str, str] = {}
    for key, label in onboard.PROVIDERS[prov].items():
        is_secret = key in ("password", "client_secret")
        env_name = _PROVIDER_ENV_HINTS.get(f"{prov}.{key}")
        env_hint = os.environ.get(env_name or "", "") if env_name else ""
        prompt_text = f"{prov} {label}" + (" (enter = env)" if env_hint else "")
        val = typer.prompt(prompt_text, default="", hide_input=is_secret)
        values[key] = val if val else env_hint
    return values


@onboard_app.command("site")
def onboard_site(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
    url: Annotated[str | None, typer.Option(help="Customer website URL (https://…)")] = None,
) -> None:
    """Step 1 — record the customer's website as local brand memory."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, slug)
        site_url = url
        if site_url is None:
            if state.json:
                raise UsageError("interactive prompt needs a terminal; pass --url <https://…>")
            site_url = typer.prompt("Customer website URL")
        return onboard.save_site(ws, slug, site_url)

    return _guard(run)


@onboard_app.command("fetch")
def onboard_fetch(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
) -> None:
    """Step 2 — crawl the recorded website (plain HTTP, no key) and run the baseline SEO audit."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, slug)
        return onboard.fetch_site(ws, slug)

    return _guard(run)


@onboard_app.command("status")
def onboard_status(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
) -> None:
    """Show onboarding progress for a brand."""

    def run() -> dict:
        ws, _ = _open()
        return onboard.site_status(ws, slug)

    return _guard(run)


@onboard_app.command("confirm")
def onboard_confirm(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
    approver: Annotated[str | None, typer.Option(help="Who confirms the feature summary")] = None,
) -> None:
    """Step 3 — confirm the agent-authored feature summary after customer review."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, slug)
        return onboard.confirm_features(ws, slug, approver or os.environ.get("USER", "cli"))

    return _guard(run)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


@providers_app.command("configure")
def providers_configure(
    name: Annotated[
        str | None, typer.Option(help="Provider to configure: dataforseo | reddit (default: both)")
    ] = None,
) -> None:
    """Configure provider credentials; secrets go to a chmod-600 file, then verified live."""

    def run() -> dict:
        _open()
        targets = [name] if name else list(onboard.PROVIDERS)
        results: dict[str, dict] = {}
        for prov in targets:
            results[prov] = onboard.configure_provider(state.data_dir, prov, _prompt_provider_values(prov))
        return {"providers": results}

    return _guard(run)


@providers_app.command("verify")
def providers_verify(
    name: Annotated[
        str | None, typer.Option(help="Provider to re-verify: dataforseo | reddit (default: both)")
    ] = None,
) -> None:
    """Re-verify stored credentials against the live endpoints."""

    def run() -> dict:
        _open()
        secrets = onboard.load_secrets(state.data_dir)
        targets = [name] if name else list(onboard.PROVIDERS)
        results: dict[str, dict] = {}
        for prov in targets:
            stored = secrets.get(prov) or {}
            if not all(stored.get(f) for f in onboard.PROVIDERS[prov]):
                raise UsageError(f"provider '{prov}' is not configured; run `seo-writer providers configure`")
            results[prov] = onboard.verify_provider(state.data_dir, prov, stored)
        return {"providers": results}

    return _guard(run)


@providers_app.command("status")
def providers_status() -> None:
    """Show configured/verified state for each provider (never prints secrets)."""

    def run() -> dict:
        _open()
        return onboard.provider_status(state.data_dir)

    return _guard(run)


# ---------------------------------------------------------------------------
# brand
# ---------------------------------------------------------------------------


@brand_app.command("create")
def brand_create(
    slug: Annotated[str, typer.Argument(help="Brand slug (lowercase, dashes)")],
    name: Annotated[str | None, typer.Option(help="Display name")] = None,
) -> None:
    """Register a customer brand."""

    def run() -> dict:
        _, db = _open()
        return {"brand": db.create_brand(slug, name or slug)}

    return _guard(run)


@brand_app.command("list")
def brand_list() -> None:
    """List registered brands."""

    def run() -> dict:
        _, db = _open()
        return {"brands": db.list_brands()}

    return _guard(run)


@brand_facts_app.command("import")
def brand_facts_import(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
    file: Annotated[str, typer.Argument(help="facts.yaml path")],
) -> None:
    """Import the per-brand claim ledger; invalidates stale approvals."""

    def run() -> dict:
        _, db = _open()
        brand = services.resolve_brand(db, brand_slug)
        facts = load_yaml_model(Path(file), FactsYaml, "facts.yaml")
        return import_facts(db, brand, facts)

    return _guard(run)


@brand_facts_app.command("show")
def brand_facts_show(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
) -> None:
    """Show the latest imported fact pack."""

    def run() -> dict:
        _, db = _open()
        brand = services.resolve_brand(db, brand_slug)
        return {"brand": brand["slug"], "facts": services.load_facts(db, brand["id"])}

    return _guard(run)


@brand_policy_app.command("import")
def brand_policy_import(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
    file: Annotated[str, typer.Argument(help="policy.yaml path")],
) -> None:
    """Import run policy; invalidates approvals granted under an older policy."""

    def run() -> dict:
        _, db = _open()
        brand = services.resolve_brand(db, brand_slug)
        policy = load_yaml_model(Path(file), PolicyYaml, "policy.yaml")
        return import_policy(db, brand, policy)

    return _guard(run)


@brand_policy_app.command("show")
def brand_policy_show(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
) -> None:
    """Show the imported run policy."""

    def run() -> dict:
        _, db = _open()
        brand = services.resolve_brand(db, brand_slug)
        raw = db.get_policy(brand["id"])
        if raw is None:
            raise UsageError(
                f"brand '{brand_slug}' has no policy.yaml imported (`seo-writer brand policy import`)"
            )
        return {"brand": brand["slug"], "policy": raw}

    return _guard(run)


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


@project_app.command("create")
def project_create(
    brand_slug: Annotated[str, typer.Argument(help="Brand slug")],
    slug: Annotated[str, typer.Argument(help="Project slug")],
    title: Annotated[str | None, typer.Option(help="Display title")] = None,
) -> None:
    """Create a project under a brand."""

    def run() -> dict:
        _, db = _open()
        return services.create_project(db, brand_slug, slug, title)

    return _guard(run)


@project_app.command("list")
def project_list(
    brand_slug: Annotated[str | None, typer.Argument(help="Filter by brand slug (optional)")] = None,
) -> None:
    """List projects, optionally filtered by brand."""

    def run() -> dict:
        _, db = _open()
        brand_id = None
        if brand_slug:
            brand_id = services.resolve_brand(db, brand_slug)["id"]
        return {"projects": db.list_projects(brand_id)}

    return _guard(run)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


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
        return services.run_research(db, run, policy, key=idempotency_key)

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
        return services.run_outline(db, run, brand, policy, key=idempotency_key, from_file=from_file)

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
        return services.run_draft(db, run, brand, policy, key=idempotency_key, from_file=from_file)

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
        return services.run_metadata(db, run, brand, policy, key=idempotency_key, from_file=from_file)

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
    format: Annotated[str, typer.Option("--format", help="Export format (Phase 1: markdown)")] = "markdown",
    out_dir: Annotated[str | None, typer.Option(help="Copy article.md + manifest.json here")] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deterministic key (defaults to run:{id}:export)")
    ] = None,
) -> None:
    """Export a completed run: article.md + traceable manifest.json."""

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
        return services.run_retry(db, run, brand, step, policy)

    return _guard(run)


# ---------------------------------------------------------------------------
# gsc (Google Search Console closed loop)
# ---------------------------------------------------------------------------


def _gsc_prompt(message: str) -> str:
    """Interactive prompt; refused under --json (the CLI stays scriptable)."""
    if state.json:
        raise UsageError("interactive setup needs a terminal; drop --json or pass the required options")
    return typer.prompt(message)


@gsc_app.command("setup")
def gsc_setup(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    client_json: Annotated[
        str | None, typer.Option(help="Path to a Desktop-app client json (path B)")
    ] = None,
) -> None:
    """Detect the auth path and guide setup: gcloud ADC (path A) or own client (path B)."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.setup_guide(
            db, ws, brand, client_json=client_json, interactive=not state.json, prompt=_gsc_prompt
        )

    return _guard(run)


@gsc_app.command("auth")
def gsc_auth(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    no_launch_browser: Annotated[bool, typer.Option(help="Print the URL and paste the code back")] = False,
) -> None:
    """One-time browser authorization (path A: gcloud login; path B: PKCE desktop flow)."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.run_auth(
            db, ws, brand, no_launch_browser=no_launch_browser, prompt=_gsc_prompt if not state.json else None
        )

    return _guard(run)


@gsc_app.command("sites")
def gsc_sites(
    brand: Annotated[str, typer.Option(help="Brand slug")],
) -> None:
    """List the GSC properties the authorized account can see."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        creds, _ = gsc.load_credentials(db, ws, brand)
        return gsc.list_sites(creds)

    return _guard(run)


@gsc_app.command("connect")
def gsc_connect(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    property: Annotated[
        str, typer.Option(help="Property URL, e.g. https://www.example.com/ or sc-domain:example.com")
    ],
) -> None:
    """Bind a GSC property to the brand (verified against the account's sites)."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.connect_property(db, ws, brand, property)

    return _guard(run)


@gsc_app.command("pull")
def gsc_pull(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    start_date: Annotated[str | None, typer.Option(help="YYYY-MM-DD (default: 30 days back)")] = None,
    end_date: Annotated[
        str | None, typer.Option(help="YYYY-MM-DD (default: today minus 3d freshness)")
    ] = None,
    force: Annotated[bool, typer.Option(help="Re-pull dates already synced")] = False,
) -> None:
    """Pull (date,query) + (date,page) rows per day; idempotent, paginated, backoff-protected."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.pull_search_analytics(db, ws, brand, start_date=start_date, end_date=end_date, force=force)

    return _guard(run)


@gsc_app.command("inspect")
def gsc_inspect(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    url: Annotated[str, typer.Option(help="URL to inspect")],
) -> None:
    """Check one URL's index coverage via URL Inspection."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.inspect_url(db, ws, brand, url)

    return _guard(run)


@gsc_app.command("import")
def gsc_import(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    csv_file: Annotated[
        str, typer.Argument(help="GSC UI export CSV (Query/Page + metrics)")
    ],
    property: Annotated[
        str | None, typer.Option(help="Property URL when the brand has no connected property")
    ] = None,
) -> None:
    """Import a dated GSC UI export CSV (fallback path C, no credentials needed)."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.import_gsc_csv(db, ws, brand, csv_file, property_url=property)

    return _guard(run)


@gsc_app.command("insights")
def gsc_insights(
    brand: Annotated[str, typer.Option(help="Brand slug")],
    window: Annotated[int, typer.Option(help="Look-back days (default 28)")] = 28,
    url: Annotated[str | None, typer.Option(help="URL performance vs audit baseline")] = None,
) -> None:
    """Closed-loop reports: high impressions/low CTR, rising queries, URL performance."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.insights(db, ws, brand, window=window, url=url)

    return _guard(run)


@gsc_app.command("status")
def gsc_status(
    brand: Annotated[str, typer.Option(help="Brand slug")],
) -> None:
    """Show brand ↔ property binding, sync range and credential path state (no secrets)."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, brand)
        return gsc.gsc_status(db, ws, brand)

    return _guard(run)
