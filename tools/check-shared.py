"""Verify independently shipped copies of the small shared modules.

Pass the sibling checkout optionally to compare both manifests as well.
"""
from pathlib import Path
import ast
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]


def normalized_ast(value):
    """Stable semantic fields across Python minors, including new empty fields."""
    if isinstance(value, ast.AST):
        return [type(value).__name__, {
            name: normalized_ast(item) for name, item in ast.iter_fields(value)
            if (item is not None and item != []) or (isinstance(value, ast.Constant) and name == 'value')
        }]
    if isinstance(value, list): return [normalized_ast(item) for item in value]
    if isinstance(value, bytes): return {'bytes': value.hex()}
    if isinstance(value, complex): return {'complex': [value.real, value.imag]}
    if value is Ellipsis: return {'ellipsis': True}
    return value


def normalized_digest(tree):
    return hashlib.sha256(json.dumps(normalized_ast(tree), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify(root):
    app = "bd2hevc_app" if (root / "bd2hevc_app").is_dir() else "dvd2hevc_app"
    manifest = json.loads((root / "SHARED_MODULES.json").read_text())
    for entry in manifest["modules"]:
        tree = ast.parse((root / app / entry["module"]).read_text(encoding="utf-8"))
        if entry["module"] == "gui_support.py":
            tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom) or node.module not in {"tools","subprocess_utils"}]
        digest = normalized_digest(tree)
        assert digest == entry["normalized_ast_sha256"], f"Update both projects and SHARED_MODULES.json: {entry['module']}"
    return manifest["modules"]


if __name__ == "__main__":
    entries = verify(ROOT)
    if len(sys.argv) > 1: assert entries == verify(Path(sys.argv[1]).resolve()), "Shared module copies have drifted"
    print("Shared module ownership/parity verified")
