"""Re-classify accounts the OLD code wrote as credential_dead for vendor code 11140.

Why a data fix and not only a code fix: the 12 affected rows already carry
error_class='credential_dead'. The new self-heal only fires for
'account_restricted', so those rows stay blocked until the label is corrected.

Safety:
  * backs up the database before touching it (timestamped, same folder);
  * touches ONLY rows where provider='codebuddy' AND error_code='11140'
    AND error_class='credential_dead' -- nothing else is read or written;
  * status is left at needs_reauth on purpose: is_available() will move it to
    cooling by itself once every lock is expired, so the account still has to
    prove itself with a real request before it is trusted;
  * prints a before/after table so the change is auditable, and --dry-run shows
    exactly what would change without writing anything.

Usage:
    python scripts/reclassify_vendor_restricted.py --dry-run
    python scripts/reclassify_vendor_restricted.py --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = os.path.join(os.path.dirname(__file__), "..", "data", "engrix_router.sqlite3")
OLD_CLASS = "credential_dead"
NEW_CLASS = "account_restricted"
VENDOR_CODE = "11140"

SELECT_SQL = """
    SELECT c.priority, h.connection_id, h.test_status, h.error_class
    FROM connections c
    JOIN connection_health h ON h.connection_id = c.id
    WHERE c.provider = 'codebuddy'
      AND h.error_code = ?
      AND h.error_class = ?
    ORDER BY c.priority
"""

UPDATE_SQL = """
    UPDATE connection_health
    SET error_class = ?, updated_at = ?
    WHERE connection_id = ?
      AND error_code = ?
      AND error_class = ?
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    group.add_argument("--apply", action="store_true", help="write the new class")
    args = parser.parse_args()

    path = os.path.abspath(DB)
    if not os.path.exists(path):
        print(f"database not found: {path}")
        return 2

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(SELECT_SQL, (VENDOR_CODE, OLD_CLASS)).fetchall()

    print(f"database : {path}")
    print(f"matching : {len(rows)} account(s) with error_code={VENDOR_CODE} "
          f"class={OLD_CLASS}\n")
    for row in rows:
        print(f"  prio {row['priority']:3d}  status={row['test_status']:14s} "
              f"{OLD_CLASS} -> {NEW_CLASS}")

    if not rows:
        print("\nnothing to do")
        con.close()
        return 0

    if args.dry_run:
        print("\nDRY RUN: no rows written")
        con.close()
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.backup-restricted-{stamp}"
    shutil.copyfile(path, backup)
    print(f"\nbackup written: {backup}")

    now = int(datetime.now().timestamp() * 1000)
    with con:
        changed = 0
        for row in rows:
            changed += con.execute(
                UPDATE_SQL, (NEW_CLASS, now, row["connection_id"], VENDOR_CODE, OLD_CLASS)
            ).rowcount
    con.close()
    print(f"rows updated: {changed}")
    print("\nNote: test_status stays needs_reauth on purpose -- is_available() moves it to "
          "cooling once the lock has expired, so the account must still succeed on a real "
          "request before it is trusted again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
