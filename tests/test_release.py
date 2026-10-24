from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util
import hashlib
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("files_release", ROOT / "scripts/package.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_allowlisted_artifact_imports_all_modules_with_real_sdk(self):
        with TemporaryDirectory() as directory:
            archive, digest = release.build(directory)
            self.assertEqual(release.verify(archive), digest)
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn("neko-files/config.toml", package.namelist())
                self.assertNotIn("neko-files/RECOVERED_SOURCE.json", package.namelist())

    def test_missing_module_or_private_member_fails_even_with_matching_checksum(self):
        for extra, remove in [
            ("neko-files/config.toml", ""),
            ("", "neko-files/neko_files/jobs.py"),
        ]:
            with TemporaryDirectory() as directory:
                archive, _ = release.build(directory)
                with zipfile.ZipFile(archive) as source:
                    members = {n: source.read(n) for n in source.namelist() if n != remove}
                if extra:
                    members[extra] = b"synthetic-private-sentinel"
                with zipfile.ZipFile(archive, "w") as destination:
                    for name, data in members.items():
                        destination.writestr(name, data)
                archive.with_suffix(".sha256").write_text(
                    hashlib.sha256(archive.read_bytes()).hexdigest(), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    release.verify(archive)

    def test_domain_does_not_import_sdk_or_host(self):
        import ast

        for name in release.MODULES:
            if name in ["plugin", "config"]:
                continue
            tree = ast.parse((ROOT / "neko_files" / (name + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("maibot_sdk", "src.")))
