"""gsc command group: Google Search Console closed loop (measure → iterate).

Module name is ``gsc_cmd`` (not ``gsc``) so ``from .. import gsc`` inside this
file unambiguously refers to the ``seo_writer.gsc`` service module.
"""

from __future__ import annotations

from typing import Annotated

import typer

from .. import gsc, services
from . import gsc_app
from ._common import _gsc_prompt, _guard, _open, state


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
    csv_file: Annotated[str, typer.Argument(help="GSC UI export CSV (Query/Page + metrics)")],
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
