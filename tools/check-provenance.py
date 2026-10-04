"""Check the bundled author against its checked-in source/binary inventory."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
directory = ROOT / "tools/hadris-udf"
manifest = json.loads((directory / "BUILD-PROVENANCE.json").read_text())
for entry in manifest["files"]:
    path = directory / entry["path"]
    assert path.is_file(), entry["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["path"]
print("Bundled author source, lockfiles and binary hashes verified")
