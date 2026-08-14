"""brand command group (incl. nested facts/policy groups): brand management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .. import services
from ..config import load_yaml_model
from ..errors import UsageError
from ..facts import import_facts
from ..models import FactsYaml, PolicyYaml
from ..policy import import_policy
from . import brand_app, brand_facts_app, brand_policy_app
from ._common import _guard, _open


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
