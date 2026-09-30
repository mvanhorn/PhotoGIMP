"""Packager coverage for PhotoGIMP release archives."""

from __future__ import annotations

import hashlib
import importlib.util
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "package_release.py"
SPEC = importlib.util.spec_from_file_location("package_release", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
package_release = importlib.util.module_from_spec(SPEC)
sys.modules["package_release"] = package_release
SPEC.loader.exec_module(package_release)

VERSION = "3.0"
FINDER_SPELLINGS = (".DS_Store", ".DS_store")
PLATFORMS = ("linux", "windows", "macos")

CONFIG_BYTES = b"gimprc-bytes\n"
HIDDEN_BYTES = b"legitimate-dotfile\n"
KEEP_BYTES = b"nested-dotfile\n"
NOTES_BYTES = b"DS_Store.txt is not Finder metadata\n"
SPLASH_BYTES = b"splash-bytes\n"
DESKTOP_BYTES = b"[Desktop Entry]\nName=PhotoGIMP\n"
ICON_BYTES = b"icon-bytes\n"
ICON_HIDDEN_BYTES = b"legitimate-icon-dotfile\n"
INSTALLER_BYTES = b"#!/bin/sh\nexit 0\n"

GIMPRC_MODE = 0o640
HIDDEN_MODE = 0o604
INSTALLER_MODE = 0o755
DEFAULT_MODE = 0o644

CONFIG_RELATIVE = (
    ".hidden",
    "DS_Store.txt",
    "gimprc",
    "splashes/.keep",
    "splashes/splash.png",
)
ICON_RELATIVE = (
    "hicolor/.hidden-icon",
    "hicolor/16x16/apps/photogimp.png",
)


class PackageReleaseTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.config_root = self.root / ".config" / "GIMP"
        self.config = self.config_root / VERSION
        self.icons = self.root / ".local" / "share" / "icons"
        self.desktop = self.root / ".local" / "share" / "applications" / "org.gimp.GIMP.desktop"
        self.installer = self.root / "install.sh"
        patcher = mock.patch.multiple(
            package_release,
            ROOT=self.root,
            CONFIG_ROOT=self.config_root,
            LINUX_DESKTOP=self.desktop,
            LINUX_ICONS=self.icons,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, path: Path, data: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def _place_finder_file(self, directory: Path, spelling: str) -> Path:
        """Store one Finder spelling, including on case-insensitive volumes.

        Those volumes keep a single directory entry per folded name, so the
        second spelling is written under ``alt-case/`` instead of overwriting
        the first.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / spelling
        if path.exists():
            stored = next(entry for entry in directory.iterdir() if entry.is_file() and entry.samefile(path))
            if stored.name != spelling:
                directory = directory / "alt-case"
                directory.mkdir(exist_ok=True)
                path = directory / spelling
        path.write_bytes(f"{path.as_posix()} {spelling}\n".encode())
        path.chmod(DEFAULT_MODE)
        stored = next(entry for entry in path.parent.iterdir() if entry.is_file() and entry.samefile(path))
        if stored.name != spelling:
            raise AssertionError(f"expected stored name {spelling}, got {stored.name}")
        return stored

    def _seed_finder_tree(self, locations: tuple[Path, ...]) -> list[Path]:
        created = []
        for location in locations:
            for spelling in FINDER_SPELLINGS:
                created.append(self._place_finder_file(location, spelling))
        return created

    def _content_locations(self) -> tuple[Path, ...]:
        return (
            self.config,
            self.config / "splashes",
            self.icons,
            self.icons / "hicolor",
            self.icons / "hicolor" / "16x16" / "apps",
        )

    def _seed_content(self, *, installer: bool, metadata: bool) -> list[Path]:
        self._write(self.config / "gimprc", CONFIG_BYTES, GIMPRC_MODE)
        self._write(self.config / ".hidden", HIDDEN_BYTES, HIDDEN_MODE)
        self._write(self.config / "DS_Store.txt", NOTES_BYTES, DEFAULT_MODE)
        self._write(self.config / "splashes" / ".keep", KEEP_BYTES, DEFAULT_MODE)
        self._write(self.config / "splashes" / "splash.png", SPLASH_BYTES, DEFAULT_MODE)
        self._write(self.desktop, DESKTOP_BYTES, DEFAULT_MODE)
        self._write(self.icons / "hicolor" / ".hidden-icon", ICON_HIDDEN_BYTES, DEFAULT_MODE)
        self._write(
            self.icons / "hicolor" / "16x16" / "apps" / "photogimp.png",
            ICON_BYTES,
            DEFAULT_MODE,
        )
        if installer:
            self._write(self.installer, INSTALLER_BYTES, INSTALLER_MODE)
        if not metadata:
            return []
        created = self._seed_finder_tree(self._content_locations())
        self._assert_both_spellings_exist(self.config)
        self._assert_both_spellings_exist(self.icons)
        return created

    def _seed_metadata_only(self) -> list[Path]:
        self._write(self.desktop, DESKTOP_BYTES, DEFAULT_MODE)
        created = self._seed_finder_tree(
            (
                self.config,
                self.config / "splashes",
                self.icons,
                self.icons / "hicolor",
            )
        )
        self._assert_both_spellings_exist(self.config)
        self._assert_both_spellings_exist(self.icons)
        return created

    def _assert_both_spellings_exist(self, root: Path) -> None:
        names = {path.name for path in root.rglob("*") if path.is_file()}
        for spelling in FINDER_SPELLINGS:
            self.assertIn(spelling, names)

    def _metadata_snapshot(self) -> dict[str, tuple[bytes, int, int]]:
        snapshot = {}
        for base in (self.config, self.icons):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file() or path.name.casefold() != ".ds_store":
                    continue
                info = path.stat()
                snapshot[path.relative_to(self.root).as_posix()] = (
                    path.read_bytes(),
                    info.st_mtime_ns,
                    stat.S_IMODE(info.st_mode),
                )
        return snapshot

    def _pack(self, platform: str, filename: str | None = None) -> Path:
        output = self.root / "archives" / (filename or f"{platform}.zip")
        output.parent.mkdir(parents=True, exist_ok=True)
        package_release.build_archive(output, platform, VERSION, self.config)
        return output

    def _assert_no_finder_members(self, archive: zipfile.ZipFile) -> None:
        basenames = [Path(member).name for member in archive.namelist()]
        for spelling in FINDER_SPELLINGS:
            self.assertNotIn(spelling, basenames)
        folded = {name.casefold() for name in basenames}
        self.assertNotIn(".ds_store", folded)

    def _assert_archive_integrity(self, archive: zipfile.ZipFile) -> None:
        self.assertIsNone(archive.testzip())
        for info in archive.infolist():
            self.assertEqual(info.date_time, package_release.FIXED_TIME)
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
        self._assert_no_finder_members(archive)

    def _expected_names(self, platform: str, *, installer: bool) -> list[str]:
        if platform == "linux":
            config_root = f"PhotoGIMP-linux/.config/GIMP/{VERSION}"
            names = [f"{config_root}/{relative}" for relative in CONFIG_RELATIVE]
            names.append("PhotoGIMP-linux/.local/share/applications/org.gimp.GIMP.desktop")
            names.extend(f"PhotoGIMP-linux/.local/share/icons/{relative}" for relative in ICON_RELATIVE)
            if installer:
                names.append("PhotoGIMP-linux/install.sh")
            return names
        if platform in ("windows", "macos"):
            return [f"{VERSION}/{relative}" for relative in CONFIG_RELATIVE]
        raise AssertionError(f"unexpected platform {platform}")

    def _assert_preserved_mode(self, archive: zipfile.ZipFile, member: str, source: Path, mode: int) -> None:
        stored = stat.S_IMODE(source.stat().st_mode)
        self.assertEqual(stored, mode)
        self.assertEqual(archive.getinfo(member).external_attr, (stored & 0xFFFF) << 16)

    def _assert_content(self, archive: zipfile.ZipFile, platform: str, *, installer: bool) -> None:
        names = archive.namelist()
        self.assertEqual(names, self._expected_names(platform, installer=installer))
        if platform == "linux":
            config_root = f"PhotoGIMP-linux/.config/GIMP/{VERSION}"
            self.assertEqual({Path(name).parts[0] for name in names}, {"PhotoGIMP-linux"})
            self.assertEqual(archive.read(f"{config_root}/gimprc"), CONFIG_BYTES)
            self.assertEqual(archive.read(f"{config_root}/.hidden"), HIDDEN_BYTES)
            self.assertEqual(archive.read(f"{config_root}/splashes/.keep"), KEEP_BYTES)
            self.assertEqual(archive.read(f"{config_root}/DS_Store.txt"), NOTES_BYTES)
            self.assertEqual(archive.read(f"{config_root}/splashes/splash.png"), SPLASH_BYTES)
            desktop_member = "PhotoGIMP-linux/.local/share/applications/org.gimp.GIMP.desktop"
            icon_member = "PhotoGIMP-linux/.local/share/icons/hicolor/16x16/apps/photogimp.png"
            hidden_icon_member = "PhotoGIMP-linux/.local/share/icons/hicolor/.hidden-icon"
            self.assertEqual(archive.read(desktop_member), DESKTOP_BYTES)
            self.assertEqual(archive.read(icon_member), ICON_BYTES)
            self.assertEqual(archive.read(hidden_icon_member), ICON_HIDDEN_BYTES)
            self._assert_preserved_mode(archive, f"{config_root}/gimprc", self.config / "gimprc", GIMPRC_MODE)
            self._assert_preserved_mode(archive, f"{config_root}/.hidden", self.config / ".hidden", HIDDEN_MODE)
            self._assert_preserved_mode(archive, desktop_member, self.desktop, DEFAULT_MODE)
            self._assert_preserved_mode(
                archive,
                icon_member,
                self.icons / "hicolor" / "16x16" / "apps" / "photogimp.png",
                DEFAULT_MODE,
            )
            if installer:
                installer_member = "PhotoGIMP-linux/install.sh"
                self.assertEqual(archive.read(installer_member), INSTALLER_BYTES)
                self._assert_preserved_mode(archive, installer_member, self.installer, INSTALLER_MODE)
            else:
                self.assertFalse(any(name.endswith("/install.sh") for name in names))
            return

        self.assertEqual({Path(name).parts[0] for name in names}, {VERSION})
        self.assertEqual(archive.read(f"{VERSION}/gimprc"), CONFIG_BYTES)
        self.assertEqual(archive.read(f"{VERSION}/.hidden"), HIDDEN_BYTES)
        self.assertEqual(archive.read(f"{VERSION}/splashes/.keep"), KEEP_BYTES)
        self.assertEqual(archive.read(f"{VERSION}/DS_Store.txt"), NOTES_BYTES)
        self.assertEqual(archive.read(f"{VERSION}/splashes/splash.png"), SPLASH_BYTES)
        self._assert_preserved_mode(archive, f"{VERSION}/gimprc", self.config / "gimprc", GIMPRC_MODE)
        self._assert_preserved_mode(archive, f"{VERSION}/.hidden", self.config / ".hidden", HIDDEN_MODE)
        self.assertFalse(any(Path(name).name == "install.sh" for name in names))
        self.assertFalse(any(Path(name).name == self.desktop.name for name in names))
        self.assertFalse(any("icons" in Path(name).parts for name in names))

    def _assert_platform_archives(self, *, installer: bool) -> None:
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                with zipfile.ZipFile(self._pack(platform)) as archive:
                    self._assert_archive_integrity(archive)
                    self._assert_content(archive, platform, installer=installer)

    def test_platform_archives_omit_finder_metadata(self):
        metadata_files = self._seed_content(installer=True, metadata=True)
        before = self._metadata_snapshot()
        self.assertEqual(len(before), len(metadata_files))
        self._assert_platform_archives(installer=True)
        self.assertEqual(self._metadata_snapshot(), before)
        for path in metadata_files:
            self.assertTrue(path.is_file())

    def test_clean_directories_produce_valid_archives(self):
        self._seed_content(installer=True, metadata=False)
        self.assertEqual(self._metadata_snapshot(), {})
        self._assert_platform_archives(installer=True)

    def test_metadata_only_directories_produce_valid_archives(self):
        metadata_files = self._seed_metadata_only()
        before = self._metadata_snapshot()
        self.assertEqual(before.keys(), {path.relative_to(self.root).as_posix() for path in metadata_files})
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                with zipfile.ZipFile(self._pack(platform)) as archive:
                    self._assert_archive_integrity(archive)
                    if platform == "linux":
                        desktop_member = "PhotoGIMP-linux/.local/share/applications/org.gimp.GIMP.desktop"
                        self.assertEqual(archive.namelist(), [desktop_member])
                        self.assertEqual(archive.read(desktop_member), DESKTOP_BYTES)
                    else:
                        self.assertEqual(archive.namelist(), [])
        self.assertEqual(self._metadata_snapshot(), before)

    def test_linux_omits_installer_when_absent(self):
        self._seed_content(installer=False, metadata=True)
        self.assertFalse(self.installer.exists())
        self._assert_platform_archives(installer=False)

    def test_repeated_builds_are_byte_identical(self):
        self._seed_content(installer=True, metadata=True)
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                first = self._pack(platform, f"{platform}-first.zip")
                second = self._pack(platform, f"{platform}-second.zip")
                self.assertEqual(first.read_bytes(), second.read_bytes())
                with zipfile.ZipFile(first) as archive:
                    self._assert_archive_integrity(archive)
                    self.assertEqual(archive.namelist(), self._expected_names(platform, installer=True))

    def test_main_checksum_file_matches_archives(self):
        metadata_files = self._seed_content(installer=True, metadata=True)
        before = self._metadata_snapshot()
        output = self.root / "dist"
        with mock.patch.object(sys, "argv", ["package_release.py", "--output", str(output)]):
            package_release.main()

        names = ("PhotoGIMP-linux.zip", "PhotoGIMP-windows.zip", "PhotoGIMP-macos.zip")
        lines = []
        for name in names:
            archive_path = output / name
            self.assertTrue(archive_path.is_file())
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}\n")
            with zipfile.ZipFile(archive_path) as archive:
                self._assert_archive_integrity(archive)
                platform = name.removeprefix("PhotoGIMP-").removesuffix(".zip")
                self._assert_content(archive, platform, installer=True)
        checksum_path = output / "SHA256SUMS.txt"
        self.assertEqual(checksum_path.read_text(encoding="utf-8"), "".join(lines))
        self.assertEqual(self._metadata_snapshot(), before)
        for path in metadata_files:
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
