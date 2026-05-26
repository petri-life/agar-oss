#!/usr/bin/env python3
"""
Agar API key management

Usage:
  uv run agar-keys create-key <label> [credits=3]
  uv run agar-keys list-keys
  uv run agar-keys revoke-key <key>
  uv run agar-keys topup <key> <credits>
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone

from api.db import (
    DB_PATH, init_db, create_key, get_key, list_keys,
    revoke_key, topup_key,
)


def cmd_create(label: str, credits: str = "3") -> None:
    init_db()
    key = "agar-" + secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()
    create_key(key, label, now, int(credits))
    print(f"key:     {key}")
    print(f"label:   {label}")
    print(f"credits: {credits}")


def cmd_list() -> None:
    init_db()
    rows = list_keys()
    if not rows:
        print("No keys.")
        return
    print(f"{'label':<20} {'credits':>7}  {'status':<8}  key")
    print("-" * 72)
    for r in rows:
        status = "revoked" if r["revoked"] else "active"
        print(f"{r['label']:<20} {r['credits_remaining']:>7}  {status:<8}  {r['key']}")


def cmd_revoke(key_or_label: str) -> None:
    init_db()
    key = _resolve_key(key_or_label)
    revoke_key(key)
    print(f"Revoked: {key}")


def _resolve_key(key_or_label: str) -> str:
    """Resolve a key or label to an API key string."""
    row = get_key(key_or_label)
    if row:
        return key_or_label
    # Search by label
    from api.db import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key, label FROM api_keys WHERE label = ?", (key_or_label,)
        ).fetchall()
    if len(rows) == 1:
        return rows[0]["key"]
    if len(rows) > 1:
        print(f"Multiple keys with label '{key_or_label}':")
        for r in rows:
            print(f"  {r['key']}")
        sys.exit(1)
    print(f"Key or label not found: {key_or_label}")
    sys.exit(1)


def cmd_topup(key_or_label: str, credits: str) -> None:
    init_db()
    key = _resolve_key(key_or_label)
    topup_key(key, int(credits))
    updated = get_key(key)
    print(f"Topped up: +{credits} → {updated['credits_remaining']} remaining ({updated['label']})")


COMMANDS = {
    "create-key": (cmd_create, (1, 2), "create-key <label> [credits=3]"),
    "list-keys":  (cmd_list,   (0, 0), "list-keys"),
    "revoke-key": (cmd_revoke, (1, 1), "revoke-key <key|label>"),
    "topup":      (cmd_topup,  (2, 2), "topup <key|label> <credits>"),
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage:")
        for _, (_, _, usage) in COMMANDS.items():
            print(f"  uv run agar-keys {usage}")
        sys.exit(1)

    cmd = sys.argv[1]
    fn, nargs, usage = COMMANDS[cmd]
    args = sys.argv[2:]
    min_args, max_args = nargs
    if not (min_args <= len(args) <= max_args):
        print(f"Usage: uv run agar-keys {usage}")
        sys.exit(1)

    fn(*args)


if __name__ == "__main__":
    main()
