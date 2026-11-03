"""Local-only allowlisted source artifact and isolated real SDK discovery."""

from pathlib import Path
from tempfile import TemporaryDirectory

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "__init__",
    "plugin",
    "config",
    "authorization",
    "archives",
    "cache",
    "jobs",
    "reading",
    "store",
    "transport",
    "worker",
    "service",
    "sources",
    "ocr",
)
FILES = (
    "plugin.py",
    "_manifest.json",
    "config.example.toml",
    "LICENSE",
    "NOTICE.md",
    "README.md",
    *(f"neko_files/{name}.py" for name in MODULES),
)


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / "neko-files-0.3.3-local.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid source member: " + name)
            archive.writestr("neko-files/" + name, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(digest + "\n", encoding="utf-8")
    return target, digest


def verify(target):
    target = Path(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if target.with_suffix(".sha256").read_text(encoding="utf-8").strip() != digest:
        raise ValueError("Artifact checksum mismatch")
    with TemporaryDirectory(prefix="files-source-") as directory:
        with zipfile.ZipFile(target) as archive:
            if len(archive.namelist()) != len(FILES) or set(archive.namelist()) != {
                "neko-files/" + name for name in FILES
            }:
                raise ValueError("Non-allowlisted or missing artifact members")
            archive.extractall(directory)
        root = Path(directory) / "neko-files"
        program = """from pathlib import Path
from types import ModuleType
import importlib,json,sys
root=Path(sys.argv[1]).resolve()
package=ModuleType('isolated_files');package.__path__=[str(root)];sys.modules[package.__name__]=package
entry=importlib.import_module(package.__name__+'.plugin')
from maibot_sdk.components import collect_components
plugin=entry.create_plugin()
tools=[item for item in collect_components(plugin) if item['type']=='TOOL']
assert len(tools)==4 and plugin.config_model().plugin.enabled is False
assert plugin.config_model().plugin.config_version=='1.0.0'
for name in json.loads(sys.argv[2]):
 module=importlib.import_module(package.__name__+'.neko_files.'+name)
 assert Path(module.__file__).resolve().is_relative_to(root)
assert all(item['metadata']['trusted_context']=='neko.tools.context.v1' for item in tools)
print('Isolated source/SDK/config discovery verified; no on_load or capability calls')
"""
        subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", program, str(root), json.dumps(MODULES)],
            cwd=directory,
            check=True,
        )
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    archive, digest = build(args.output)
    verify(archive)
    print(json.dumps({"archive": str(archive), "sha256": digest, "public_release": False}))
