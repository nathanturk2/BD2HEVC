"""Build reviewable release artifacts from a positive source-file manifest."""
from __future__ import annotations
import argparse
import ast
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = "bd2hevc_app" if (ROOT / "bd2hevc_app").is_dir() else "dvd2hevc_app"
NAME = "BD2HEVC" if APP == "bd2hevc_app" else "DVD2HEVC"


def version():
    for path in (ROOT / APP / "__init__.py", ROOT / APP / "config.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if any(isinstance(target, ast.Name) and target.id in {"VERSION", "__version__"} for target in node.targets):
                    return str(node.value.value)
    raise RuntimeError("No single-source release version found")


def source_files():
    root_files = {".gitignore", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "setup.py", "MANIFEST.in", "bd2hevc.py", "bd_to_uhdbd.py", "dvd2hevc.py", "requirements.txt", "SHARED_MODULES.json"}
    extensions = {".py", ".pyw", ".ps1", ".cs", ".c", ".java", ".rs", ".toml", ".lock", ".patch", ".md", ".json", ".yml", ".yaml", ".txt", ".png", ".jpg", ".ico", ".svg", ".sh"}
    directories = {APP, "dvd2uhd", "assets", "docs", "tests", "tools", "scripts", "native", "patches", "examples", ".github"}
    root_files.add(".gitattributes")
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.is_symlink(): continue
        relative = path.relative_to(ROOT)
        if any(part in {".git", "__pycache__", "target", "build", "dist", "work", "reports", "resources"} for part in relative.parts): continue
        allowed = len(relative.parts) == 1 and path.name in root_files
        if relative.parts[0] in directories and path.suffix.lower() in extensions: allowed = True
        if relative.parts[0] in directories and path.name.startswith(("LICENSE", "COPYING")): allowed = True
        if relative.as_posix() == 'dvd2uhd/NOTICE': allowed = True
        if relative.as_posix() == "tools/hadris-udf/bin/hadris-udf.exe": allowed = True
        if relative.as_posix().startswith("tools/hadris-udf/") and path.name == "Cargo.toml.orig": allowed = True
        if path.name.startswith((".cargo", ".crates")) or path.name == "HANDOFF.md": allowed = False
        if allowed: yield path, relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--require-clean", action="store_true", help="Require a clean release commit before exporting")
    args = parser.parse_args()
    destination = args.output_dir.resolve(); destination.mkdir(parents=True, exist_ok=True)
    release_version = version()
    revision = subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True)
    status = subprocess.run(["git","status","--porcelain"],cwd=ROOT,capture_output=True,text=True)
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    if args.require_clean and (revision.returncode or dirty):
        raise RuntimeError("Commit the intended release and verify a clean checkout first")
    committed = None
    if args.require_clean:
        committed = tarfile.open(fileobj=io.BytesIO(subprocess.check_output(
            ["git", "archive", "--format=tar", revision.stdout.strip()], cwd=ROOT)))
    with tempfile.TemporaryDirectory(prefix="hevc-release-") as temporary:
        export = Path(temporary) / f"{NAME}-{release_version}"
        export.mkdir()
        manifest = []
        for source, relative in source_files():
            target = export / relative; target.parent.mkdir(parents=True, exist_ok=True)
            data = committed.extractfile(relative.as_posix()).read() if committed else source.read_bytes()
            target.write_bytes(data)
            manifest.append({"path": relative.as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if committed: committed.close()
        (export / "SOURCE-MANIFEST.json").write_text(json.dumps({"schema":"hevc-source-release-v1", "version":release_version,
            "git_revision":revision.stdout.strip() if revision.returncode == 0 else None,"working_tree_dirty":dirty,"files":manifest}, indent=2), encoding="utf-8")
        source_zip = destination / f"{NAME}-{release_version}-source.zip"
        with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(export.rglob("*")):
                if path.is_file(): archive.write(path, f"{export.name}/{path.relative_to(export).as_posix()}")
        subprocess.run([sys.executable,"-c","import setuptools.build_meta as b; b.build_wheel('package-dist'); b.build_sdist('package-dist')"],cwd=export,check=True)
        for path in (export / "package-dist").iterdir(): shutil.copy2(path, destination / path.name)
    for path in sorted(destination.iterdir()):
        if path.is_file(): print(path.name, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__": main()
