"""Build the same complete runtime resources used by source checkouts."""
from pathlib import Path
import shutil
import os
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

PACKAGE = 'bd2hevc_app'
class build_py(_build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / PACKAGE / "resources"
        directories = ['assets', 'docs', 'examples', 'presets']
        for directory in directories:
            source = root / directory
            if source.is_dir():
                shutil.copytree(source, destination / directory, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "target", "*.pyc", "*.obj", "*.log"))
        for relative in ["tools/hadris-udf", "tools/wb-jukebox"]:
            source = root / relative
            if source.is_dir():
                shutil.copytree(source, destination / relative, dirs_exist_ok=True, ignore=shutil.ignore_patterns("target", "__pycache__", ".crates*", ".cargo*", *([] if os.name == "nt" else ["*.exe"])))
        for source in (root / "tools").glob("*"):
            if source.is_file() and source.suffix in {".ps1", ".cs", ".py"}:
                (destination / "tools").mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / "tools" / source.name)
        for name in ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'bd2hevc.py', 'bd_to_uhdbd.py']:
            source = root / name
            if source.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / name)
class bdist_wheel(_bdist_wheel):
    def get_tag(self):
        if os.name == "nt": return "py3", "none", "win_amd64"
        return super().get_tag()
setup(cmdclass={"build_py": build_py, "bdist_wheel": bdist_wheel})
