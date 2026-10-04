"""Verify independently shipped copies of the small shared modules.

Pass the sibling checkout optionally to compare both manifests as well.
"""
from pathlib import Path
import ast
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify(root):
    app = "bd2hevc_app" if (root / "bd2hevc_app").is_dir() else "dvd2hevc_app"
    manifest = json.loads((root / "SHARED_MODULES.json").read_text())
    for entry in manifest["modules"]:
        tree = ast.parse((root / app / entry["module"]).read_text(encoding="utf-8"))
        if entry["module"] == "gui_support.py":
            tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom) or node.module not in {"tools","subprocess_utils"}]
        digest = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
        assert digest == entry["normalized_ast_sha256"], f"Update both projects and SHARED_MODULES.json: {entry['module']}"
    return manifest["modules"]


if __name__ == "__main__":
    entries = verify(ROOT)
    if len(sys.argv) > 1: assert entries == verify(Path(sys.argv[1]).resolve()), "Shared module copies have drifted"
    print("Shared module ownership/parity verified")
