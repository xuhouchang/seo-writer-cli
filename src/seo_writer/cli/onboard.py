"""onboard command group: new-brand onboarding (site memory, crawl, audit, confirmation)."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from .. import onboard, services
from ..errors import UsageError
from . import onboard_app
from ._common import _guard, _open, state


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
    approver: Annotated[str | None, typer.Option(help="Who confirms the product evidence brief")] = None,
) -> None:
    """Step 3 — confirm the agent-authored product evidence brief after customer review."""

    def run() -> dict:
        ws, db = _open()
        services.resolve_brand(db, slug)
        return onboard.confirm_features(ws, slug, approver or os.environ.get("USER", "cli"))

    return _guard(run)


@onboard_app.command("brand-profile")
def onboard_brand_profile(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
    out_dir: Annotated[str | None, typer.Option(help="Also copy review artifacts here")] = None,
) -> None:
    """Generate an English-only factual brand profile review HTML and JSON source."""

    def run() -> dict:
        ws, db = _open()
        brand = services.resolve_brand(db, slug)
        return services.generate_brand_profile_review(ws, db, brand, out_dir=out_dir)

    return _guard(run)


@onboard_app.command("import-brand-profile")
def onboard_import_brand_profile(
    slug: Annotated[str, typer.Argument(help="Brand slug")],
    review_json: Annotated[str, typer.Argument(help="Downloaded brand-profile review JSON")],
) -> None:
    """Import a current factual review; stale revision or input hashes are rejected."""

    def run() -> dict:
        ws, db = _open()
        brand = services.resolve_brand(db, slug)
        return services.import_brand_profile_review(ws, db, brand, review_json)

    return _guard(run)
