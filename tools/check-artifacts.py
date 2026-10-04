"""Verify manifests, exported-source tests, launcher and a fresh wheel install."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd, env, log):
    with log.open("ab") as output:
        result = subprocess.run([str(item) for item in command],cwd=cwd,env=env,stdout=output,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),timeout=600)
    if result.returncode:
        print(log.read_text(encoding="utf-8",errors="replace")[-12000:])
        raise RuntimeError(f"Artifact check failed ({result.returncode})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip",type=Path)
    parser.add_argument("--wheel",type=Path)
    args = parser.parse_args()
    archive = (args.source_zip or max((ROOT/"dist").glob("*-source.zip"),key=lambda p:p.stat().st_mtime)).resolve()
    wheel = (args.wheel or max((ROOT/"dist").glob("*.whl"),key=lambda p:p.stat().st_mtime)).resolve()
    bd = archive.name.startswith("BD2HEVC-")
    app,command = ("bd2hevc_app","bd2hevc") if bd else ("dvd2hevc_app","dvd2hevc")
    with tempfile.TemporaryDirectory(prefix="hevc-artifact-check-") as temporary:
        directory = Path(temporary).resolve()
        log = directory/"checks.log"
        with zipfile.ZipFile(archive) as contents:
            names = contents.namelist()
            for name in names:
                target=(directory/name).resolve()
                if not target.is_relative_to(directory): raise ValueError("Archive path escapes its extraction root")
            root_name=names[0].split("/")[0]
            manifest=json.loads(contents.read(root_name+"/SOURCE-MANIFEST.json"))
            expected={root_name+"/"+item["path"] for item in manifest["files"]}
            assert set(names)==expected|{root_name+"/SOURCE-MANIFEST.json"},"Unexpected exported files"
            for item in manifest["files"]:
                data=contents.read(root_name+"/"+item["path"])
                assert len(data)==item["size"] and hashlib.sha256(data).hexdigest()==item["sha256"],item["path"]
                path=Path(item["path"])
                assert not any(part in {"work","reports",".git","target","__pycache__"} for part in path.parts),item["path"]
                assert path.suffix.lower() not in {".iso",".m2ts",".vob",".log",".dll"},item["path"]
                if path.suffix.lower()==".exe":assert item["path"]=="tools/hadris-udf/bin/hadris-udf.exe"
            contents.extractall(directory)
        export=directory/root_name
        environment=os.environ.copy()
        environment.pop("PYTHONPATH",None)
        environment["BD2HEVC_STATE_DIR" if bd else "DVD2HEVC_STATE_DIR"]=str(directory/"state")
        run([sys.executable,"-m","unittest","discover","-s","tests"],export,environment,log)
        run([sys.executable,"tools/check-shared.py"],export,environment,log)
        run([sys.executable,"tools/check-provenance.py"],export,environment,log)
        if os.name=="nt":
            run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",export/"tools/build-gui-launcher.ps1",
                 "-Destination",directory/(command+".exe")],export,environment,log)
        run([sys.executable,"-m","venv",directory/"venv"],directory,environment,log)
        python=directory/"venv"/("Scripts/python.exe" if os.name=="nt" else "bin/python")
        run([python,"-m","pip","install",wheel],directory,environment,log)
        neutral=directory/"neutral";neutral.mkdir()
        check=neutral/"check-installed.py";shutil.copy2(export/"tools/check-installed.py",check)
        run([python,check,app],neutral,environment,log)
        run([python,"-m",app,"--help"],neutral,environment,log)
        executable=python.parent/(command+".exe" if os.name=="nt" else command)
        run([executable,"auto","--help"],neutral,environment,log)
        gui_executable=python.parent/(command+"-gui.exe" if os.name=="nt" else command+"-gui")
        assert gui_executable.is_file(),"Installed GUI entry point missing"
        if os.name=="nt":
            gui_class="BD2HEVCApp" if bd else "DVD2HEVCApp"
            run([python,"-c",f"from {app}.gui import {gui_class}; app={gui_class}(); app.withdraw(); app.update(); app.destroy()"],neutral,environment,log)
        receipt={"schema":"hevc-artifact-check-v1","version":manifest["version"],"git_revision":manifest.get("git_revision"),
            "source_manifest_verified":True,"exported_source_tests_passed":True,"wheel_installed_outside_checkout":True,
            "installed_resources_verified":True,"entry_points_verified":True,"gui_entry_point_verified":True,"windows_gui_verified":os.name=="nt",
            "source_zip_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"wheel_sha256":hashlib.sha256(wheel.read_bytes()).hexdigest()}
        (archive.parent/"artifact-check.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
        print(json.dumps(receipt,indent=2))


if __name__=="__main__":main()
