"""project command group: project management."""

from __future__ import annotations

from typing import Annotated

import typer

from .. import services
from . import project_app
from ._common import _guard, _open


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
