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
import sys
from typing import Annotated

import typer
from typer import _click as click

from .. import RULES_VERSION, __version__
from ..config import resolve_data_dir, resolve_workspace
from ._common import _guard, _open, state

app = typer.Typer(help="Local-first, gate-governed SEO content production CLI.", invoke_without_command=True)
brand_app = typer.Typer(help="Brand management.", no_args_is_help=True)
brand_facts_app = typer.Typer(help="Per-brand fact ledger.", no_args_is_help=True)
brand_policy_app = typer.Typer(help="Per-brand run policy.", no_args_is_help=True)
project_app = typer.Typer(help="Project management.", no_args_is_help=True)
run_app = typer.Typer(help="ArticleRun lifecycle.", no_args_is_help=True)
onboard_app = typer.Typer(
    help="New-brand onboarding (site memory, crawl, audit, confirmation).",
    no_args_is_help=True,
)
providers_app = typer.Typer(
    help="Provider credential configuration (DataForSEO, Reddit).",
    no_args_is_help=True,
)
gsc_app = typer.Typer(help="Google Search Console closed loop (measure → iterate).", no_args_is_help=True)

app.add_typer(brand_app, name="brand")
brand_app.add_typer(brand_facts_app, name="facts")
brand_app.add_typer(brand_policy_app, name="policy")
app.add_typer(project_app, name="project")
app.add_typer(run_app, name="run")
app.add_typer(onboard_app, name="onboard")
app.add_typer(providers_app, name="providers")
app.add_typer(gsc_app, name="gsc")


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


# Import the per-group command modules so their @<group>_app.command()
# decorators register. Must stay at the bottom: the modules import the app
# objects defined above, so this file's app/state must already exist.
from . import brand, gsc_cmd, onboard, project, providers  # noqa: E402,F401
from . import run as run_cmd  # noqa: E402,F401
