"""Offline backup CLI; never imports app config or loads a dotenv file.

Stop every process using the DB/session before acknowledging --services-stopped.
Only the offers DB is snapshotted with SQLite's backup API; other files are copied.
"""

from __future__ import annotations

import argparse
from contextlib import closing, suppress
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile


class BackupError(Exception):
    """An actionable error safe to display without source paths or secrets."""


def _required_file(path: Path, label: str) -> Path:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{label} must be an existing regular file, not a symlink.")
    return path.resolve()


def _snapshot(source_path: Path, snapshot_path: Path) -> None:
    # mode=ro prevents a typo from silently creating an empty source database.
    uri = source_path.as_uri() + "?mode=ro"
    deadline = time.monotonic() + 30

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError("SQLite snapshot timed out; confirm every service is stopped.")

    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source:
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            source.backup(snapshot, pages=256, progress=progress, sleep=0.05)
            # Produce one standalone DB even if the source uses WAL.
            snapshot.execute("PRAGMA journal_mode=DELETE")
            if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise BackupError("SQLite snapshot failed its integrity check.")


def _archive_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"offers-backup-{stamp}.zip"


def _write_member(archive: zipfile.ZipFile, path: Path, name: str) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname=name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
        shutil.copyfileobj(source, target)


def create_backup(
    destination: Path,
    *,
    db_path: Path = Path("data/offers.db"),
    session_path: Path | None = Path("data/listener_session"),
    env_file: Path | None = None,
    media_dir: Path | None = None,
    services_stopped: bool = False,
) -> Path:
    """Create a private, exclusive ZIP with canonical relative member names.

    The acknowledgement is mandatory, not an automatic process/lock check.
    Missing sessions are allowed; explicitly requested env/media paths must exist.
    """
    if not services_stopped:
        raise BackupError(
            "Stop ALL services and local processes, then pass --services-stopped; "
            "/pause alone is not sufficient."
        )

    database = _required_file(db_path, "Database")
    files: list[tuple[Path, str]] = []
    if session_path is not None:
        session = Path(session_path).expanduser()
        if session.suffix != ".session":
            session = Path(str(session) + ".session")
        session_files = [session] + [
            Path(str(session) + suffix) for suffix in ("-journal", "-wal", "-shm")
        ]
        if not session.exists() and any(path.exists() for path in session_files[1:]):
            raise BackupError("Session sidecars exist without the session; inspect before backing up.")
        for path in session_files:
            if path.exists() or path.is_symlink():
                suffix = str(path)[len(str(session)):]
                files.append((
                    _required_file(path, "Session"),
                    "data/listener_session.session" + suffix,
                ))
    if env_file is not None:
        files.append((_required_file(env_file, "Environment file"), ".env"))

    destination = Path(destination).expanduser().resolve()
    if media_dir is not None:
        media = Path(media_dir).expanduser()
        if (
            media.is_symlink()
            or getattr(media, "is_junction", lambda: False)()
            or not media.is_dir()
        ):
            raise BackupError("Media directory must exist and must not be a symlink or junction.")
        media = media.resolve()
        if destination.is_relative_to(media):
            raise BackupError("Destination must be outside the media directory.")
        if any(path.is_relative_to(media) for path in [database, *(p for p, _ in files)]):
            raise BackupError("Media directory must not contain the DB, session or environment file.")
        def media_walk_error(error: OSError) -> None:
            raise BackupError(
                "Cannot read media directory; check read permissions before backing up."
            ) from error

        media_paths: list[Path] = []
        for directory, directories, filenames in os.walk(
            media, topdown=True, onerror=media_walk_error, followlinks=False,
        ):
            directories.sort()
            for entry in directories + sorted(filenames):
                path = Path(directory) / entry
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise BackupError("Media must not contain symlinks or directory junctions.")
                if not path.is_dir():
                    media_paths.append(path)
        for path in sorted(media_paths):
            name = path.relative_to(media).as_posix()
            # Backslashes/colons are unsafe on Windows even when created on Unix.
            if "\\" in name or ":" in name:
                raise BackupError("Media has a filename unsafe for cross-platform restore.")
            files.append((_required_file(path, "Media file"), "data/media/" + name))

    sources = [database, *(path for path, _ in files)]
    if len(set(sources)) != len(sources):
        raise BackupError("Backup sources must be distinct files.")
    archive_path = destination / _archive_name()
    if archive_path in sources:
        raise BackupError("Destination archive must not be a source file.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix="offers-backup-") as temporary:
        snapshot = Path(temporary) / "offers.db"
        _snapshot(database, snapshot)
        # Exclusive creation also protects existing files, hardlinks and symlinks.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(archive_path, flags, 0o600)
        complete = False
        try:
            with os.fdopen(fd, "wb") as output:
                with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
                    _write_member(archive, snapshot, "data/offers.db")
                    for path, name in files:
                        _write_member(archive, path, name)
                output.flush()
                os.fsync(output.fileno())
            complete = True
        finally:
            if not complete:
                with suppress(FileNotFoundError):
                    archive_path.unlink()
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a sensitive offline backup ZIP")
    parser.add_argument(
        "--services-stopped", action="store_true", required=True,
        help="acknowledge ALL app processes are stopped (not merely /pause)",
    )
    parser.add_argument("--destination", type=Path, default=Path("data/backups"))
    parser.add_argument("--db-path", type=Path, default=Path("data/offers.db"))
    parser.add_argument(
        "--session-path", type=Path, default=Path("data/listener_session"),
        help="session basename or .session file; missing sessions are skipped",
    )
    parser.add_argument(
        "--env-file", type=Path, default=None,
        help="opt in to copying this file as .env; never parsed or printed",
    )
    parser.add_argument("--media-dir", type=Path, default=None, help="optional existing media tree")
    args = parser.parse_args(argv)
    try:
        archive = create_backup(
            args.destination,
            db_path=args.db_path,
            session_path=args.session_path,
            env_file=args.env_file,
            media_dir=args.media_dir,
            services_stopped=args.services_stopped,
        )
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError):
        # OS/SQLite errors may contain private paths; do not echo their raw text.
        print(
            "Backup failed: check paths, permissions, free space, database health, "
            "and that all services are stopped. No existing archive was overwritten.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Backup interrupted; no complete archive was produced.", file=sys.stderr)
        return 130
    print(f"Backup created: {archive.name} in the selected destination.")
    print("Sensitive, unencrypted archive: restrict access and encrypt before transfer/storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
