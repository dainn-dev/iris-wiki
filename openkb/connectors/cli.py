"""Click group: ``openkb connector gdrive ...``.

Registered from ``openkb.cli`` so that module is not grown with Drive logic.
"""

from __future__ import annotations

from pathlib import Path

import click


def register_connector_commands(cli) -> None:
    """Attach the ``connector`` group to the root Click command."""

    @cli.group()
    def connector():
        """Cloud document-source connectors."""

    @connector.group()
    def gdrive():
        """Google Drive folder sync."""

    def _kb_dir(ctx) -> Path:
        from openkb.cli import _find_kb_dir

        kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override") if ctx.obj else None)
        if kb_dir is None:
            click.echo("No knowledge base found. Run `openkb init` first.", err=True)
            ctx.exit(1)
        return kb_dir

    @gdrive.command("connect-sa")
    @click.option("--folder", "folder_id", required=True, help="Drive folder id to sync.")
    @click.option(
        "--json",
        "json_path",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Path to a Google service-account JSON key.",
    )
    @click.pass_context
    def connect_sa(ctx, folder_id: str, json_path: Path):
        """Connect this KB with a service-account key and start folder sync."""
        from openkb.connectors.gdrive import GdriveError, get_folder_name
        from openkb.connectors.store import (
            GdriveStoreError,
            load_state,
            parse_service_account_json,
            save_state,
            set_refresh_token,
            set_service_account_json,
        )
        from openkb.connectors.sync_service import enable_folder, sync_once

        kb_dir = _kb_dir(ctx)
        try:
            info = parse_service_account_json(json_path.read_text(encoding="utf-8"))
            set_service_account_json(kb_dir, info)
            set_refresh_token(kb_dir, None)
            disk = load_state(kb_dir)
            disk.auth_mode = "service_account"
            save_state(kb_dir, disk)
            name = get_folder_name(kb_dir, folder_id)
            enable_folder(kb_dir, folder_id, name)
        except (GdriveError, GdriveStoreError, OSError) as exc:
            click.echo(str(exc), err=True)
            ctx.exit(1)
        click.echo(f"Connected Google Drive folder {name!r} ({folder_id}).")
        result = sync_once(kb_dir)
        click.echo(
            "Sync: added={added} skipped={skipped} failed={failed} removed={removed}".format(
                **result
            )
        )

    @gdrive.command("status")
    @click.pass_context
    def status_cmd(ctx):
        """Show this KB's Google Drive connector status (no secrets)."""
        from openkb.connectors.store import public_status

        kb_dir = _kb_dir(ctx)
        payload = public_status(kb_dir)
        for key, value in payload.items():
            click.echo(f"{key}: {value}")

    @gdrive.command("sync")
    @click.pass_context
    def sync_cmd(ctx):
        """Run one Google Drive folder poll now."""
        from openkb.connectors.gdrive import GdriveError
        from openkb.connectors.sync_service import sync_once

        kb_dir = _kb_dir(ctx)
        try:
            result = sync_once(kb_dir)
        except GdriveError as exc:
            click.echo(str(exc), err=True)
            ctx.exit(1)
        click.echo(
            "Sync: added={added} skipped={skipped} failed={failed} removed={removed}".format(
                **result
            )
        )

    @gdrive.command("disconnect")
    @click.pass_context
    def disconnect_cmd(ctx):
        """Remove Drive credentials and connector state for this KB."""
        from openkb.connectors.store import clear_connection

        kb_dir = _kb_dir(ctx)
        clear_connection(kb_dir)
        click.echo("Google Drive connector disconnected.")
