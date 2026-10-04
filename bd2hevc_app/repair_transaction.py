"""Publish repaired media and CLPI together, with rollback and recovery."""
from __future__ import annotations
import json
import os
import shutil
import tempfile
from pathlib import Path
from .tools import ToolError


def recover_repairs(output_clip: Path, output_clpi: Path) -> None:
    allowed = {output_clip.resolve(), output_clpi.resolve()}
    for folder in output_clip.parent.glob(".bd2hevc-repair-*"):
        journal = folder / "journal.json"
        if not journal.is_file(): continue
        record = json.loads(journal.read_text(encoding="utf-8"))
        rows = record.get("files", [])
        if {Path(row["destination"]).resolve() for row in rows} != allowed: continue
        if record.get("state") != "committed":
            for row in rows:
                destination, backup = Path(row["destination"]), folder / row["backup"]
                if backup.resolve().parent != folder.resolve(): raise ToolError("Unsafe repair journal")
                if row["existed"]:
                    shutil.copy2(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
        shutil.rmtree(folder)


def publish_repair(temp_clip: Path, temp_clpi: Path, output_clip: Path, output_clpi: Path) -> dict:
    recover_repairs(output_clip, output_clpi)
    folder = Path(tempfile.mkdtemp(prefix=".bd2hevc-repair-", dir=output_clip.parent))
    rows = []
    try:
        for index, destination in enumerate((output_clip, output_clpi)):
            row = {"destination": str(destination.resolve()), "backup": str(index), "existed": destination.exists()}
            if row["existed"]: shutil.copy2(destination, folder / str(index))
            rows.append(row)
        journal = folder / "journal.json"
        journal.write_text(json.dumps({"state":"prepared", "files":rows}), encoding="utf-8")
        os.replace(temp_clip, output_clip)
        output_clpi.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_clpi, output_clpi)
        journal.write_text(json.dumps({"state":"committed", "files":rows}), encoding="utf-8")
    except BaseException:
        if (folder / "journal.json").exists(): recover_repairs(output_clip, output_clpi)
        raise
    finally:
        if folder.exists() and (folder / "journal.json").exists(): recover_repairs(output_clip, output_clpi)
    return {"ok": True, "transactional": True}
