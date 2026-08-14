"""providers command group: provider credential configuration (DataForSEO, Reddit)."""

from __future__ import annotations

from typing import Annotated

import typer

from .. import onboard
from ..errors import UsageError
from . import providers_app
from ._common import _guard, _open, _prompt_provider_values, state


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
