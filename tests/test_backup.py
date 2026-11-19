"""Offline backup tests: every input/output is synthetic and under tmp_path."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import zipfile

import pytest

from scripts import backup


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO offers (title) VALUES ('Synthetic offer')")
        connection.commit()
    return path


def make_backup(tmp_path: Path, database: Path, **overrides) -> Path:
    options = {
        "db_path": database,
        "session_path": tmp_path / "absent_session",
        "services_stopped": True,
    }
    options.update(overrides)
    return backup.create_backup(tmp_path / "backups", **options)


def cli_args(tmp_path: Path, database: Path) -> list[str]:
    return [
        "--services-stopped",
        "--destination", str(tmp_path / "backups"),
        "--db-path", str(database),
        "--session-path", str(tmp_path / "absent_session"),
    ]


def restored_rows(tmp_path: Path, archive_path: Path) -> list[tuple]:
    restored = tmp_path / "restored.db"
    with zipfile.ZipFile(archive_path) as archive:
        restored.write_bytes(archive.read("data/offers.db"))
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        return connection.execute("SELECT * FROM offers ORDER BY id").fetchall()


def test_acknowledgement_required_before_any_io(tmp_path, monkeypatch):
    def unexpected_read(*args):
        pytest.fail("sources must not be read without the acknowledgement")

    monkeypatch.setattr(backup, "_required_file", unexpected_read)
    with pytest.raises(backup.BackupError, match="Stop ALL services"):
        backup.create_backup(tmp_path / "backups", db_path=tmp_path / "missing.db")
    assert not (tmp_path / "backups").exists()


def test_cli_requires_stopped_flag_without_touching_defaults(monkeypatch, capsys):
    def unexpected_backup(*args, **kwargs):
        pytest.fail("CLI must reject before touching any default source path")

    monkeypatch.setattr(backup, "create_backup", unexpected_backup)
    with pytest.raises(SystemExit) as error:
        backup.main([])
    assert error.value.code == 2
    assert "--services-stopped" in capsys.readouterr().err


def test_snapshot_is_standalone_and_session_can_be_missing(tmp_path, database):
    archive_path = make_backup(tmp_path, database)
    assert archive_path.name.startswith("offers-backup-")
    assert archive_path.name.endswith("Z.zip")
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["data/offers.db"]
        assert archive.testzip() is None
    assert restored_rows(tmp_path, archive_path) == [(1, "Synthetic offer")]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone() == (1,)


def test_snapshot_includes_committed_wal_data(tmp_path):
    database = tmp_path / "wal-source.db"
    # Keep a synthetic connection open so WAL data is not checkpointed on close.
    # No application process or background writer runs during this test.
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO offers VALUES (1, 'Committed in WAL')")
        connection.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        archive_path = make_backup(tmp_path, database, session_path=None)
        assert restored_rows(tmp_path, archive_path) == [(1, "Committed in WAL")]
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.namelist() == ["data/offers.db"]


@pytest.mark.parametrize("use_suffix", [False, True])
def test_session_config_and_sidecars_use_fixed_safe_names(tmp_path, database, use_suffix):
    private = tmp_path / "private-source"
    private.mkdir()
    session = private / "custom-login.session"
    session.write_bytes(b"synthetic session bytes")
    sidecars = {suffix: suffix.encode() for suffix in ("-journal", "-wal", "-shm")}
    for suffix, content in sidecars.items():
        Path(str(session) + suffix).write_bytes(content)
    config = private / "private-config"
    config.write_bytes(b"BOT_TOKEN=not-a-real-token\n")
    archive_path = make_backup(
        tmp_path, database,
        session_path=session if use_suffix else session.with_suffix(""),
        env_file=config,
    )
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("data/listener_session.session") == session.read_bytes()
        assert archive.read(".env") == config.read_bytes()
        for suffix, content in sidecars.items():
            assert archive.read("data/listener_session.session" + suffix) == content
        assert len(archive.namelist()) == 6
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert "\\" not in member.filename
            assert ":" not in member.filename
            assert "private-source" not in member.filename
            assert stat.S_IMODE(member.external_attr >> 16) == 0o600


def test_env_is_opt_in_not_discovered_from_working_directory(tmp_path, database, monkeypatch):
    # This is a fake temp-only config, never the repository's real .env.
    (tmp_path / ".env").write_text("SYNTHETIC_SECRET=not-real\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    archive_path = make_backup(tmp_path, database)
    with zipfile.ZipFile(archive_path) as archive:
        assert ".env" not in archive.namelist()


@pytest.mark.parametrize("source_kind", ["missing", "directory", "corrupt"])
def test_bad_database_does_not_create_an_archive(tmp_path, source_kind):
    database = tmp_path / "invalid.db"
    if source_kind == "directory":
        database.mkdir()
    elif source_kind == "corrupt":
        database.write_bytes(b"not sqlite")
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        make_backup(tmp_path, database)
    assert not list(tmp_path.rglob("*.zip"))
    if source_kind == "missing":
        assert not database.exists()


@pytest.mark.parametrize("option", ["env_file", "media_dir"])
def test_explicit_missing_optional_input_fails(tmp_path, database, option):
    with pytest.raises(backup.BackupError):
        make_backup(tmp_path, database, **{option: tmp_path / "does-not-exist"})
    assert not list(tmp_path.rglob("*.zip"))


def test_orphan_session_sidecars_fail(tmp_path, database):
    (tmp_path / "absent_session.session-wal").write_bytes(b"synthetic wal")
    with pytest.raises(backup.BackupError, match="sidecars exist without"):
        make_backup(tmp_path, database)
    assert not list(tmp_path.rglob("*.zip"))


def test_duplicate_sources_rejected(tmp_path, database):
    with pytest.raises(backup.BackupError, match="distinct files"):
        make_backup(tmp_path, database, env_file=database)


def test_timestamp_collision_does_not_overwrite(tmp_path, database, monkeypatch):
    monkeypatch.setattr(backup, "_archive_name", lambda: "offers-backup-fixed.zip")
    archive_path = make_backup(tmp_path, database)
    original = archive_path.read_bytes()
    with pytest.raises(FileExistsError):
        make_backup(tmp_path, database)
    assert archive_path.read_bytes() == original
    assert restored_rows(tmp_path, archive_path) == [(1, "Synthetic offer")]


def test_existing_output_hardlink_cannot_overwrite_source(tmp_path, database, monkeypatch):
    monkeypatch.setattr(backup, "_archive_name", lambda: "offers-backup-hardlink.zip")
    destination = tmp_path / "backups"
    destination.mkdir()
    output = destination / "offers-backup-hardlink.zip"
    try:
        os.link(database, output)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")
    original = database.read_bytes()
    with pytest.raises(FileExistsError):
        make_backup(tmp_path, database)
    assert database.read_bytes() == original
    assert output.read_bytes() == original


def test_output_cannot_be_the_source_file(tmp_path, database, monkeypatch):
    monkeypatch.setattr(backup, "_archive_name", lambda: "source.db")
    original = database.read_bytes()
    with pytest.raises(backup.BackupError, match="must not be a source"):
        backup.create_backup(
            tmp_path, db_path=database, session_path=None, services_stopped=True,
        )
    assert database.read_bytes() == original


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_partial_archive_is_removed(tmp_path, database, monkeypatch, failure):
    def fail_write(*args):
        raise failure("synthetic failure")

    monkeypatch.setattr(backup, "_write_member", fail_write)
    original = database.read_bytes()
    with pytest.raises(failure):
        make_backup(tmp_path, database)
    assert not list((tmp_path / "backups").glob("*.zip"))
    assert database.read_bytes() == original


def test_snapshot_timeout_does_not_leave_archive(tmp_path, database, monkeypatch):
    ticks = iter([0, 31])
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(ticks, 31))
    with pytest.raises(backup.BackupError, match="timed out"):
        make_backup(tmp_path, database)
    assert not list(tmp_path.rglob("*.zip"))


def test_optional_media_tree_uses_relative_paths_only(tmp_path, database):
    media = tmp_path / "photos"
    (media / "nested").mkdir(parents=True)
    (media / "nested" / "photo.jpg").write_bytes(b"synthetic image")
    (tmp_path / "excluded.log").write_text("not included", encoding="utf-8")
    archive_path = make_backup(tmp_path, database, media_dir=media)
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"data/offers.db", "data/media/nested/photo.jpg"}
        assert archive.read("data/media/nested/photo.jpg") == b"synthetic image"


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("through_cli", [False, True])
def test_unreadable_media_fails_before_snapshot(
    tmp_path, database, monkeypatch, capsys, nested, through_cli,
):
    media = tmp_path / "photos"
    media.mkdir()
    (media / "readable.jpg").write_bytes(b"synthetic image")
    unreadable = media / "restricted" if nested else media
    if nested:
        unreadable.mkdir()
        (unreadable / "hidden.jpg").write_bytes(b"synthetic hidden image")
    original_scandir = os.scandir

    def denied_scandir(path):
        if path == unreadable or path == str(unreadable):
            raise PermissionError("SYNTHETIC_SECRET/private-media")
        return original_scandir(path)

    def unexpected_snapshot(*args):
        pytest.fail("Unreadable media must abort before snapshot/archive creation")

    monkeypatch.setattr(backup.os, "scandir", denied_scandir)
    monkeypatch.setattr(backup, "_snapshot", unexpected_snapshot)
    if through_cli:
        assert backup.main(cli_args(tmp_path, database) + ["--media-dir", str(media)]) == 1
        output = capsys.readouterr()
        assert "Backup created:" not in output.out
        message = output.err
    else:
        with pytest.raises(backup.BackupError) as error:
            make_backup(tmp_path, database, media_dir=media)
        message = str(error.value)
    assert "Cannot read media directory" in message
    assert "SYNTHETIC_SECRET" not in message
    assert str(tmp_path) not in message
    assert not (tmp_path / "backups").exists()


def test_media_archive_order_is_deterministic(tmp_path, database, monkeypatch):
    media = tmp_path / "photos"
    names = ["z-dir/z.jpg", "z-root.jpg", "a-dir/z.jpg", "a-dir/a.jpg", "a-root.jpg"]
    for name in names:
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic image")
    original_walk = os.walk

    def reversed_walk(*args, **kwargs):
        for directory, directories, filenames in original_walk(*args, **kwargs):
            directories.sort(reverse=True)
            filenames.sort(reverse=True)
            yield directory, directories, filenames

    monkeypatch.setattr(backup.os, "walk", reversed_walk)
    archive_path = make_backup(tmp_path, database, media_dir=media)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["data/offers.db"] + [
            "data/media/" + name for name in sorted(names)
        ]


@pytest.mark.parametrize("link_method", ["is_symlink", "is_junction"])
def test_media_directory_links_rejected_before_descent(
    tmp_path, database, monkeypatch, link_method,
):
    media = tmp_path / "photos"
    linked_directory = media / "linked"
    linked_directory.mkdir(parents=True)
    original_scandir = os.scandir

    def guarded_scandir(path):
        if path == linked_directory or path == str(linked_directory):
            pytest.fail("Must reject linked directories before descending into them")
        return original_scandir(path)

    monkeypatch.setattr(
        type(media), link_method, lambda path: path == linked_directory, raising=False,
    )
    monkeypatch.setattr(backup.os, "scandir", guarded_scandir)
    with pytest.raises(backup.BackupError, match="symlinks or directory junctions"):
        make_backup(tmp_path, database, media_dir=media)
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_destination_inside_media_rejected(tmp_path, database, nested):
    media = tmp_path / "photos"
    media.mkdir()
    destination = media / "backups" if nested else media
    with pytest.raises(backup.BackupError, match="outside the media"):
        backup.create_backup(
            destination, db_path=database, session_path=None,
            media_dir=media, services_stopped=True,
        )
    assert not list(tmp_path.rglob("*.zip"))


def test_media_must_not_contain_other_sources(tmp_path, database):
    media = tmp_path / "photos"
    media.mkdir()
    nested_database = media / "offers.db"
    nested_database.write_bytes(database.read_bytes())
    with pytest.raises(backup.BackupError, match="must not contain the DB"):
        backup.create_backup(
            tmp_path / "outside", db_path=nested_database, session_path=None,
            media_dir=media, services_stopped=True,
        )


@pytest.mark.parametrize("at_root", [False, True])
def test_media_junctions_rejected(tmp_path, database, monkeypatch, at_root):
    media = tmp_path / "photos"
    media.mkdir()
    junction = media if at_root else media / "junction"
    if not at_root:
        junction.mkdir()
    # Model the junction attribute without needing Windows link privileges.
    monkeypatch.setattr(type(media), "is_junction", lambda path: path == junction, raising=False)
    with pytest.raises(backup.BackupError, match="junction"):
        make_backup(tmp_path, database, media_dir=media)
    assert not list(tmp_path.rglob("*.zip"))


def create_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this user/platform")


@pytest.mark.parametrize("input_kind", ["database", "session", "env", "media"])
def test_symlink_sources_rejected(tmp_path, database, input_kind):
    options = {}
    if input_kind == "media":
        media = tmp_path / "photos"
        media.mkdir()
        create_symlink_or_skip(media / "escape", database)
        options["media_dir"] = media
    else:
        link = tmp_path / ("linked.session" if input_kind == "session" else "linked")
        create_symlink_or_skip(link, database)
        options[{"database": "db_path", "session": "session_path", "env": "env_file"}[input_kind]] = link
    with pytest.raises(backup.BackupError, match="symlink"):
        make_backup(tmp_path, database, **options)
    assert not list(tmp_path.rglob("*.zip"))


@pytest.mark.skipif(os.name == "nt", reason="Windows disallows backslashes/colons in filenames")
@pytest.mark.parametrize("filename", ["escape\\photo.jpg", "drive:photo.jpg"])
def test_media_names_unsafe_on_windows_are_rejected(tmp_path, database, filename):
    media = tmp_path / "photos"
    media.mkdir()
    (media / filename).write_bytes(b"synthetic image")
    with pytest.raises(backup.BackupError, match="unsafe for cross-platform"):
        make_backup(tmp_path, database, media_dir=media)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not enforce Windows ACLs")
def test_posix_output_permissions_are_private(tmp_path, database):
    archive_path = make_backup(tmp_path, database)
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive_path.parent.stat().st_mode) == 0o700


def test_cli_success_does_not_print_paths_or_config(tmp_path, database, capsys):
    config = tmp_path / "private-config"
    config.write_text("BOT_TOKEN=SYNTHETIC_SECRET\n", encoding="utf-8")
    assert backup.main(cli_args(tmp_path, database) + ["--env-file", str(config)]) == 0
    output = capsys.readouterr()
    assert "Backup created:" in output.out
    assert "unencrypted" in output.out
    assert not output.err
    assert "SYNTHETIC_SECRET" not in output.out
    assert str(tmp_path) not in output.out
    assert "private-config" not in output.out
    assert len(list((tmp_path / "backups").glob("*.zip"))) == 1


def test_cli_redacts_storage_error_details(tmp_path, database, monkeypatch, capsys):
    def fail_snapshot(*args):
        raise sqlite3.OperationalError("SYNTHETIC_SECRET/private-config")

    monkeypatch.setattr(backup, "_snapshot", fail_snapshot)
    assert backup.main(cli_args(tmp_path, database)) == 1
    output = capsys.readouterr()
    assert "Backup failed:" in output.err
    assert "SYNTHETIC_SECRET" not in output.err
    assert "private-config" not in output.err
    assert "Backup created:" not in output.out
    assert not list(tmp_path.rglob("*.zip"))


def test_cli_reports_actionable_validation_error(tmp_path, database, capsys):
    assert backup.main(cli_args(tmp_path, database) + ["--env-file", str(tmp_path / "absent")]) == 1
    output = capsys.readouterr()
    assert "Environment file must be an existing regular file" in output.err
    assert str(tmp_path) not in output.err
