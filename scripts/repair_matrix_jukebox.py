"""Apply the scoped Matrix jukebox repair with reversible metadata backups."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bd2hevc_app.wb_jukebox import patch_disc

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
report = patch_disc(args.source.resolve(), dry_run=args.dry_run)
text = json.dumps(report, indent=2)
if args.report:
    args.report.write_text(text + "\n", encoding="utf-8")
print(text)
