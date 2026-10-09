"""Create or validate the SQLite schema. Run: `python scripts/bootstrap_db.py [--force]`

This is the first thing that detects a syntax error in schema.sql or a duplicated index
name, which is why it stays a separate script instead of a test.

`--force` only re-stamps a checksum that changed WITHOUT a structural migration (a comment
or a wording change, for example). If the change is a column or a table, force is the wrong
answer: bump SCHEMA_VERSION and add a MIGRATIONS entry, because force would leave every
existing database behind. That is why the flag is explicit instead of the default.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engrix_router.storage import sqlite as db
from engrix_router.core import config


def main() -> int:
    force = "--force" in sys.argv[1:]
    info = db.init_schema(force=force)
    print(f"db       : {info['path']}")
    print(f"checksum : {info['checksum']}" + ("  (re-stamped by --force)" if force else ""))
    applied = info["migrations_applied"]
    print(f"schema   : v{info['version']}" + (f"  ({len(applied)} migration steps applied)" if applied else ""))
    print(f"tables   : {len(info['tables'])}")
    for name in info["tables"]:
        print(f"  {name:<20} rows={db.count(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
