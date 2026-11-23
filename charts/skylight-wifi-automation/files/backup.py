#!/usr/bin/env python3
"""Online backup of the Skylight Wi-Fi Automation SQLite database.

The source database stays in place. This process opens it, copies it with the
sqlite3 backup API, and runs PRAGMA integrity_check on the copy. It does not
execute INSERT, UPDATE, or DELETE against the source.

A successful run prints one line:

    backup ok file=<path> bytes=<n> kept=<n>
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKUP_NAME = re.compile(r"^app-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<suffix>\d+))?\.db$")


def _fail(message: str) -> None:
    print(f"backup failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def _destination(directory: Path, stamp: str) -> Path:
    candidate = directory / f"app-{stamp}.db"
    if not candidate.exists():
        return candidate
    number = 1
    while True:
        candidate = directory / f"app-{stamp}-{number}.db"
        if not candidate.exists():
            return candidate
        number += 1


def _copy(source: Path, dest: Path) -> None:
    partial = dest.with_name(dest.name + ".partial")
    if partial.exists():
        partial.unlink()
    src = sqlite3.connect(source, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            rows = [row[0] for row in dst.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                raise RuntimeError("integrity_check failed")
            dst.commit()
        finally:
            dst.close()
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    finally:
        src.close()
    os.chmod(partial, stat.S_IRUSR | stat.S_IWUSR)
    partial.replace(dest)


def _sort_key(path: Path) -> tuple[str, int]:
    match = BACKUP_NAME.match(path.name)
    if match is None:
        return ("", 0)
    # The unsuffixed file is written first, so suffix 0 is the oldest in that second.
    return (match.group("stamp"), int(match.group("suffix") or 0))


def _prune(directory: Path, keep: int) -> int:
    files = sorted(
        (path for path in directory.glob("app-*.db") if BACKUP_NAME.match(path.name)),
        key=_sort_key,
    )
    if len(files) <= keep:
        return len(files)
    for old in files[: len(files) - keep]:
        old.unlink()
    return keep


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    backup_dir = Path(os.environ.get("BACKUP_DIR", "/backups"))
    try:
        keep = int(os.environ.get("BACKUP_KEEP", "7"))
    except ValueError:
        _fail("BACKUP_KEEP must be an integer")
    if keep < 1:
        _fail("BACKUP_KEEP must be at least 1")
    source = data_dir / "app.db"
    if not source.is_file():
        _fail(f"database not found: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = _destination(backup_dir, stamp)
    try:
        _copy(source, dest)
    except Exception as exc:  # noqa: BLE001 - the message is a sqlite error, not a secret
        _fail(str(exc))
    kept = _prune(backup_dir, keep)
    print(
        f"backup ok file={dest} bytes={dest.stat().st_size} kept={kept}",
        flush=True,
    )


if __name__ == "__main__":
    main()
