"""Build our compatibility classes against a locally supplied disc/API JAR."""
import argparse
import base64
import pprint
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--disc-jar", type=Path, required=True)
parser.add_argument("--bdj-api", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="bd2hevc-jukebox-") as folder:
    output = Path(folder)
    import os
    subprocess.run(["javac", "-source", "7", "-target", "7", "-cp",
                    os.pathsep.join(map(str, (args.disc_jar.resolve(), args.bdj_api.resolve()))),
                    "-d", str(output), *map(str, sorted(root.glob("*.java")))], check=True)
    payload = {path.relative_to(output).as_posix(): base64.b64encode(path.read_bytes()).decode("ascii")
               for path in sorted(output.rglob("*.class"))}
    target = root.parents[1] / "bd2hevc_app/_wb_jukebox_payload.py"
    target.write_text("# Generated from tools/wb-jukebox Java sources; contains only BD2HEVC compatibility code.\n"
                      + "CLASSES = " + pprint.pformat(payload, width=100) + "\n", encoding="utf-8")
    print(target)
