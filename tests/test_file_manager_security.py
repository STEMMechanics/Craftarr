from types import SimpleNamespace

import pytest

import zipfile

from app.file_manager import (
    create_file,
    create_zip,
    delete_entry,
    extract_zip,
    format_size,
    is_text_file,
    read_text_file,
    safe_path,
    write_text_file,
)


def test_safe_path_rejects_parent_traversal(tmp_path):
    server = SimpleNamespace(directory=str(tmp_path / "server"))
    (tmp_path / "server").mkdir()

    with pytest.raises(ValueError, match="Invalid path"):
        safe_path(server, "../outside.txt")


def test_safe_path_rejects_symlink_escape(tmp_path):
    root = tmp_path / "server"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    server = SimpleNamespace(directory=str(root))

    with pytest.raises(ValueError, match="Invalid path"):
        safe_path(server, "escape/secret.txt")


def test_delete_entry_refuses_server_root(tmp_path):
    server = SimpleNamespace(directory=str(tmp_path))

    with pytest.raises(ValueError, match="Cannot delete server root"):
        delete_entry(server, "")


def test_create_file_is_empty_and_does_not_replace_existing_files(tmp_path):
    root = tmp_path / "server"
    root.mkdir()
    (root / "plugins").mkdir()
    server = SimpleNamespace(directory=str(root))

    assert create_file(server, "plugins", "notes.yml") == "plugins/notes.yml"
    assert (root / "plugins/notes.yml").read_text(encoding="utf-8") == ""
    (root / "plugins/existing.yml").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        create_file(server, "plugins", "existing.yml")
    assert (root / "plugins/existing.yml").read_text(encoding="utf-8") == "keep"


def test_create_file_rejects_paths_and_parent_traversal(tmp_path):
    root = tmp_path / "server"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    server = SimpleNamespace(directory=str(root))

    with pytest.raises(ValueError, match="valid file name"):
        create_file(server, "", "../outside.txt")
    with pytest.raises(ValueError, match="Invalid path"):
        create_file(server, "escape", "secret.txt")


def test_format_size_spells_out_bytes():
    assert format_size(491) == "491 Bytes"


def test_unknown_utf8_file_requires_confirmation(tmp_path):
    (tmp_path / "notes.unknown").write_text("hello", encoding="utf-8")
    server = SimpleNamespace(directory=str(tmp_path))

    with pytest.raises(ValueError, match="Confirm"):
        read_text_file(server, "notes.unknown")

    assert read_text_file(server, "notes.unknown", allow_unknown=True) == "hello"


def test_binary_file_is_not_text(tmp_path):
    path = tmp_path / "data.unknown"
    path.write_bytes(b"abc\x00def")

    assert not is_text_file(path)


def test_create_zip_uses_next_available_dash_number(tmp_path):
    source = tmp_path / "config.yml"
    source.write_text("enabled: true", encoding="utf-8")
    (tmp_path / "config.yml.zip").touch()
    server = SimpleNamespace(directory=str(tmp_path))

    result = create_zip(server, "config.yml")

    assert result == "config.yml-2.zip"
    with zipfile.ZipFile(tmp_path / result) as archive:
        assert archive.read("config.yml") == b"enabled: true"


def test_extract_zip_merge_and_full_replace(tmp_path):
    server = SimpleNamespace(directory=str(tmp_path))
    existing = tmp_path / "pack"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    archive_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("pack/new.txt", "new")

    assert extract_zip(server, "pack.zip", "check", 1024) == ["pack"]
    extract_zip(server, "pack.zip", "merge", 1024)
    assert (existing / "keep.txt").exists()
    assert (existing / "new.txt").read_text(encoding="utf-8") == "new"

    extract_zip(server, "pack.zip", "replace", 1024)
    assert not (existing / "keep.txt").exists()
    assert (existing / "new.txt").exists()


def test_extract_zip_rejects_parent_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.txt", "no")
    server = SimpleNamespace(directory=str(tmp_path))

    with pytest.raises(ValueError, match="unsafe path"):
        extract_zip(server, "unsafe.zip", "merge", 1024)


def test_invalid_yaml_is_saved_with_a_warning(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("valid: true\n", encoding="utf-8")
    server = SimpleNamespace(directory=str(tmp_path))

    warning = write_text_file(server, "config.yml", "broken: [value\n")

    assert path.read_text(encoding="utf-8") == "broken: [value\n"
    assert warning["line"] == 2
    assert warning["column"] >= 1
    assert "YAML" in warning["message"]
