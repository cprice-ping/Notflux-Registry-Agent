"""
registry_service/app/migrate_ids.py

One-shot migration utility for Registry PIP entity IDs.

Purpose:
  Backfill legacy records to the new reversible storage-ID format used by
  id_codec.to_storage_id().

Usage:
  # Dry run (default): report what would change
  python -m app.migrate_ids

  # Apply updates
  python -m app.migrate_ids --apply
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from sqlalchemy import select, text

from .database import AsyncSessionLocal
from .id_codec import to_storage_id
from .models import Entity


@dataclass
class MigrationResult:
    scanned: int = 0
    would_update: int = 0
    updated: int = 0
    skipped_safe: int = 0
    skipped_collision: int = 0


async def migrate_ids(apply_changes: bool) -> dict[str, object]:
    """
    Migrate legacy entity IDs to storage-safe IDs.

    Rules:
    - If to_storage_id(id) == id, skip (already safe/in desired format).
    - If to_storage_id(id) differs and target ID already exists, skip collision.
    - Otherwise update entities.id from old -> new.

    Returns a summary payload suitable for JSON output.
    """
    result = MigrationResult()
    changes: list[dict[str, str]] = []
    collisions: list[dict[str, str]] = []

    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(Entity.id))
        ids = [row[0] for row in rows.all()]

        for old_id in ids:
            result.scanned += 1
            new_id = to_storage_id(old_id)

            if new_id == old_id:
                result.skipped_safe += 1
                continue

            existing = await session.get(Entity, new_id)
            if existing is not None:
                result.skipped_collision += 1
                collisions.append({"old_id": old_id, "new_id": new_id})
                continue

            result.would_update += 1
            changes.append({"old_id": old_id, "new_id": new_id})

            if apply_changes:
                # Update PK explicitly using SQL to avoid ORM identity-map edge cases.
                await session.execute(
                    text("UPDATE entities SET id = :new_id WHERE id = :old_id"),
                    {"old_id": old_id, "new_id": new_id},
                )
                result.updated += 1

        if apply_changes and result.updated > 0:
            await session.commit()

    return {
        "mode": "apply" if apply_changes else "dry-run",
        "summary": {
            "scanned": result.scanned,
            "would_update": result.would_update,
            "updated": result.updated,
            "skipped_safe": result.skipped_safe,
            "skipped_collision": result.skipped_collision,
        },
        "changes": changes,
        "collisions": collisions,
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Registry PIP entity IDs to reversible b64_ format")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Omit for dry-run.",
    )
    args = parser.parse_args()

    payload = await migrate_ids(apply_changes=args.apply)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
