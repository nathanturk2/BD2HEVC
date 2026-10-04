"""Show the real UI with labelled, simulated jobs in isolated temporary storage.

For documentation screenshots only. Conversion controls are disabled; no real
queue is read, started or changed. Use make-demo.py for actual generated-media
conversion tests.
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BD = (ROOT / "bd2hevc_app").is_dir()
temporary = tempfile.TemporaryDirectory(prefix="hevc-ui-demo-")
os.environ["BD2HEVC_STATE_DIR" if BD else "DVD2HEVC_STATE_DIR"] = temporary.name
if BD:
    from bd2hevc_app import gui
    app_type = gui.BD2HEVCApp
else:
    from dvd2hevc_app import gui
    app_type = gui.DVD2HEVCApp

kind = "BD" if BD else "DVD"
name = "BD2HEVC" if BD else "DVD2HEVC"
jobs = {}
for index, (title, state, percent) in enumerate([
    ("Colour Lab", "running", 62.4), ("Night Sky", "queued", 0),
    ("Sound Garden", "completed" if BD else "passed", 100), ("Incomplete Backup", "failed", 12.5)]):
    identifier = f"demo-{index + 1}-{title.lower().replace(' ', '-')}"
    job = {"id":identifier,"status":state,"source":f"C:/HEVC-Demo/Backups/{title} ({kind})" + ("" if BD else ".iso"),
        "output":f"C:/HEVC-Demo/Converted/{title} ({kind}) (HEVC).iso","output_format":"iso",
        "log":f"C:/HEVC-Demo/Logs/{identifier}.log","demo_percent":percent,
        "settings":{"quality":"balanced" if BD else "target-bitrate", "encoder":"libx265", "encoder_preset":"p1",
            "audio_mode":"passthrough","bitrate_mode":"vbr","target_bitrate_multiplier":1.0},
        "command":["auto","--quality","balanced","--encoder","libx265","--output-format","iso"]}
    jobs[Path(temporary.name) / f"{identifier}.job.json"] = job

gui.known_job_files = lambda: list(jobs)
gui.job_error = lambda job, *_: "Source backup is incomplete; output was not published." if job["status"] == "failed" else ""
if BD:
    gui.try_load_job = lambda path: jobs[path].copy()
    gui.job_runtime_status = lambda job: job["status"]
    gui.job_progress_snapshot = lambda job: {"overall":job["demo_percent"],"video":78.0 if job["status"] == "running" else job["demo_percent"],
        "audio":100.0 if job["status"] == "running" else job["demo_percent"],"mux":32.0 if job["status"] == "running" else job["demo_percent"],
        "stage":"encoding pipeline (simulated)","detail":{"encode_file":"00000.m2ts","encode_speed":"illustration"}}
else:
    gui.try_read_job = lambda path: jobs[path].copy()
    gui.refresh_job = lambda _, job: job
    gui.pipeline_status = lambda job: {"stage":"encoding pipeline (simulated)"}
    gui.pipeline_percent = lambda job, _: job["demo_percent"]
    gui.lane_progress_snapshot = lambda job, _: {"video":(78.0,"78% · generated title (simulated)"),
        "audio":(100.0,"source audio preserved"),"mux":(32.0,"32% · rebuilding disc layout (simulated)")}

app = app_type()
app.title(name + " — demonstration")
app.geometry("1180x860+80+40")
ttk.Label(app, text="DOCUMENTATION DEMO · generated backup names · simulated queue and progress",
    foreground="#7a4300", padding=(20,7)).pack(before=app.notebook, fill="x")
app.source_var.set("C:/HEVC-Demo/" + kind + "/Colour Lab (" + kind + ")" + ("" if BD else ".iso"))
app.output_var.set("C:/HEVC-Demo/Converted/")
if BD: app.output_format_var.set("iso")
app.busy_label.configure(text="Screenshot demonstration — controls disabled; no conversion runs in this window")

def ready():
    app._refresh_jobs()
    app.jobs_tree.selection_set(next(iter(jobs.values()))["id"])
    app._show_job_detail()
    def disable(widget):
        if isinstance(widget, ttk.Button): widget.configure(state="disabled")
        for child in widget.winfo_children(): disable(child)
    disable(app)
app.after(700, ready)
app.mainloop()
temporary.cleanup()
