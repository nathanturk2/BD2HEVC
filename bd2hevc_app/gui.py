"""Windows GUI for BD2HEVC's playlist-aware full-disc conversion engine."""

from __future__ import annotations

import argparse
import ctypes
import copy
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from .paths import REPORT_ROOT, STATE_ROOT
from typing import Any, Callable

from .bitrate import format_duration
from .gui_support import (WorkflowUI, destination_path, source_path, path_text, pack_scrollable, update_row, prune_rows, set_detail, job_error)

from .config import (
    DEFAULT_DEINTERLACE_MODE,
    DEFAULT_JOB_DIR,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_STEREO_AUDIO_BITRATE,
    HEVC_ENCODERS,
    ROOT,
    VERSION,
)
from .core import build_parser, enqueue_conversion_job
from .output import disc_title_from_folder_name, generated_output_for, safe_name
from .presets import available_presets, load_named_preset, named_preset_path, codec_ratio_cli_values
from .progress import latest_log_progress, read_text_flexible
from .queueing import (
    cmd_cancel_job,
    cmd_pause_queue,
    cmd_remove_job,
    cmd_resume_queue,
    job_runtime_status,
    known_job_files,
    load_job,
    queue_is_paused,
    queue_pause_reason,
    try_load_job,
)
from .tools import ToolError, hidden_process_kwargs, refreshed_env


APP_BG = "#eaf1f5"
PANEL = "#ffffff"
INK = "#172033"
MUTED = "#5e6b82"
HEADER = "#173b58"
HEADER_MUTED = "#c8deec"
ACCENT = "#087f8c"
ACCENT_DARK = "#075d68"
ACCENT_LIGHT = "#d9eff1"
TAB_BG = "#d8e6ec"
WARN = "#ad6400"
APP_USER_MODEL_ID = "BD2HEVC.Project.GUI.1"
WATCH_STATE = REPORT_ROOT / "gui-watched-batch.json"

QUALITY_VALUES = (
    "cq:18", "cq:20", "cq:22", "cq:24", "cq:25",
    "balanced", "smaller", "transparent", "source-ratio:0.60", "copy",
)

FIELD_HELP = {
    "Source backup": "The decrypted Blu-ray folder containing BDMV. BD2HEVC reads it but never changes it.",
    "Output backup": "The new full-disc folder. Menus, playlists, extras, subtitles, and navigation are retained while eligible video clips become HEVC.",
    "Output format": "Folder keeps the traditional BDMV directory. Blu-ray ISO wraps the same completed backup in a verified UDF 2.50 image that VLC can open directly.",
    "General quality": "Quality for eligible clips that are not overridden. CQ uses HandBrake's direction: lower is higher quality and larger.",
    "Feature override": "Main feature follows the longest real MPLS playlist and related seamless-branched cuts, so every shared and alternate feature clip receives the override.",
    "Top N clips": "Override the longest N physical M2TS clips. This is useful for episode discs whose episodes are separate clips.",
    "Encoder": "NVENC is the fast tested default. QSV and AMF require compatible hardware; libx265 is slower but more compression-efficient.",
    "Bit depth": "8-bit Main is the compatibility default for 8-bit Blu-rays. Main10 can reduce banding but has stricter player requirements.",
    "Encode ahead": "Hardware encoders can encode the next clip while compact audio, muxing, navigation repair, and validation finish for earlier clips.",
    "Deinterlace": "Auto is the default and only filters streams whose source metadata is interlaced. Off disables deinterlacing; force filters all reencoded clips.",
    "Audio": "Passthrough keeps every source track. Compact-stereo converts playable tracks to AC-3 stereo or mono while preserving track positions and metadata.",
    "Output profile": "Library uses UHD-like folders with BD-compatible navigation headers for VLC. Disc adds physical-media sizing and UHD header guardrails.",
    "MakeMKV scan": "Optional title-level validation. Normal conversion can plan directly from BDMV without probing an optical drive.",
    "Validation": "Seconds decoded from every replacement clip after muxing. More is stronger but increases post-encode time.",
    "Watch stability": "A watched backup must stop changing for this long before it is queued, avoiding folders still being written by MakeMKV.",
    "Source ratio": "Optional fixed HEVC/source bitrate multiplier. Leave blank to use the selected quality policy; this is mainly for controlled VBR experiments.",
    "Codec ratios": "Optional comma-separated codec overrides such as h264=0.55, vc1=0.45, mpeg2video=0.30. Matching clips use these ratios.",
    "Minimum bitrate": "Lower VBR clamp for very simple clips. Accepts values such as 2M or 2000k.",
    "Maximum bitrate": "Upper VBR/VBV clamp. 80M is the Blu-ray-oriented default ceiling.",
    "CQ minimum length": "With compact CQ, shorter clips use the compact VBR fallback. Accepts 10s, 15m, or 00:15:00.",
}


class ToolTip:
    def __init__(self, widget: tk.Misc, text: str, *, delay_ms: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._hide()
        self.after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self) -> None:
        self.after_id = None
        if self.window is not None or not self.widget.winfo_exists():
            return
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        label = tk.Label(
            window, text=self.text, justify="left", wraplength=390,
            bg="#fff9db", fg=INK, relief="solid", borderwidth=1,
            padx=9, pady=7, font=("Segoe UI", 9),
        )
        label.pack()
        window.update_idletasks()
        x = min(self.widget.winfo_pointerx() + 14, self.widget.winfo_screenwidth() - window.winfo_reqwidth() - 8)
        y = min(self.widget.winfo_pointery() + 18, self.widget.winfo_screenheight() - window.winfo_reqheight() - 8)
        window.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
        self.window = window

    def _hide(self, _event: Any = None) -> None:
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


def discover_disc_sources(path: Path, *, recursive: bool = False) -> list[Path]:
    path = path.expanduser().resolve()
    candidates: list[Path] = []
    if (path / "BDMV").is_dir():
        candidates.append(path)
    elif path.name.upper() == "BDMV" and path.is_dir():
        candidates.append(path.parent)
    elif path.is_dir():
        if recursive:
            candidates.extend(folder.parent for folder in path.rglob("BDMV") if folder.is_dir())
        else:
            candidates.extend(child for child in path.iterdir() if child.is_dir() and (child / "BDMV").is_dir())
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(str(candidate.resolve()).casefold(), candidate.resolve())
    return sorted(unique.values(), key=lambda value: (value.stat().st_mtime, value.name.casefold()))


def generated_output(source: Path, output_dir: Path | None = None, *, add_tags: bool = True, output_format: str = "folder") -> Path:
    output = generated_output_for(source, output_dir or source.resolve().parent, add_tags=add_tags)
    return output.with_name(output.name + ".iso") if output_format == "iso" else output


def source_fingerprint(source: Path) -> dict[str, int]:
    count = 0
    size = 0
    latest = 0
    for file in source.rglob("*"):
        if not file.is_file():
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        count += 1
        size += stat.st_size
        latest = max(latest, stat.st_mtime_ns)
    return {"files": count, "bytes": size, "latest_ns": latest}


def backup_looks_complete(source: Path) -> bool:
    bdmv = source / "BDMV"
    return all(
        path.exists()
        for path in (
            bdmv / "index.bdmv", bdmv / "MovieObject.bdmv",
            bdmv / "PLAYLIST", bdmv / "STREAM",
        )
    ) and any((bdmv / "STREAM").glob("*.m2ts"))


def command_option(job: dict[str, Any], flag: str, default: str = "") -> str:
    command = [str(value) for value in job.get("command") or []]
    try:
        return command[command.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def job_progress_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    status = job_runtime_status(job)
    if status == "completed":
        return {"overall": 100.0, "video": 100.0, "audio": 100.0, "mux": 100.0, "stage": "complete", "detail": {}}
    plan_path = Path(str(job.get("plan") or ""))
    log_path = Path(str(job.get("log") or ""))
    if not plan_path.is_file() or plan_path.stat().st_size == 0:
        return {"overall": 0.0, "video": 0.0, "audio": 0.0, "mux": 0.0, "stage": "queued" if status in {"queued", "paused"} else "planning", "detail": {}}
    try:
        plan = json.loads(read_text_flexible(plan_path))
    except (OSError, json.JSONDecodeError):
        return {"overall": 0.0, "video": 0.0, "audio": 0.0, "mux": 0.0, "stage": "planning", "detail": {}}
    clips = list(plan.get("reencode_clips") or [])
    audio_only = list(plan.get("compact_audio_remux_clips") or [])
    state = latest_log_progress(log_path)
    durations = {str(clip.get("file")): float(clip.get("duration") or 0) for clip in clips}
    total = sum(durations.values())

    def duration_percent(done_names: list[str], active_name: str | None, active_seconds: float) -> float:
        done = sum(durations.get(name, 0.0) for name in set(done_names))
        if active_name and active_name not in set(done_names):
            done += min(durations.get(active_name, 0.0), max(0.0, active_seconds))
        return max(0.0, min(100.0, done / total * 100.0 if total else 100.0))

    video = duration_percent(
        list(state.get("encoded_files") or []),
        state.get("encode_file"), float(state.get("encode_seconds") or 0),
    )
    audio_mode = str((plan.get("audio") or {}).get("mode") or command_option(job, "--audio-mode", "passthrough"))
    if audio_mode == "compact-stereo":
        audio = duration_percent(
            list(state.get("audio_done_files") or []),
            state.get("audio_file"), float(state.get("audio_seconds") or 0),
        )
        if not clips:
            audio = 0.0
    else:
        audio = video if status == "running" else 0.0
    mux = duration_percent(
        list(state.get("done_files") or []),
        state.get("mux_file"),
        durations.get(str(state.get("mux_file")), 0.0) * float(state.get("mux_percent") or 0) / 100.0,
    )
    if audio_only:
        audio_only_names = {str(item.get("file")) for item in audio_only}
        audio_only_done = set(state.get("audio_remux_done_files") or [])
        tail = len(audio_only_done & audio_only_names) / len(audio_only_names) * 100.0
        audio = min(100.0, audio * 0.9 + tail * 0.1)
        mux = min(100.0, mux * 0.9 + tail * 0.1)
    if audio_mode == "compact-stereo":
        overall = video * 0.68 + audio * 0.10 + mux * 0.22
    else:
        overall = video * 0.76 + mux * 0.24
    if str(job.get("output_format") or command_option(job, "--output-format", "folder")) == "iso":
        overall *= 0.96
        if state.get("iso_author_started") and not state.get("iso_author_done"):
            overall = max(overall, 97.0)
    if status in {"failed", "canceled"}:
        stage = status
    else:
        stage = str(state.get("current_stage") or ("finalizing" if video >= 99.9 else "encoding pipeline"))
    return {
        "overall": max(0.0, min(99.8, overall)),
        "video": video, "audio": audio, "mux": mux,
        "stage": stage, "detail": state,
    }


def watch_conversion_flags(flags: list[str]) -> list[str]:
    """Discard per-disc overrides and replacement flags from older saved watches."""
    result = []
    index = 0
    while index < len(flags):
        flag = flags[index]
        index += 1
        if flag == "--force":
            continue
        if flag == "--clip-quality":
            index += 2
            continue
        if flag in {"--copy-clips", "--deinterlace-clips", "--no-deinterlace-clips"}:
            while index < len(flags) and not flags[index].startswith("--"):
                index += 1
            continue
        result.append(flag)
    return result


class BD2HEVCApp(WorkflowUI, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._init_workflow("BD")
        self.title("BD2HEVC")
        self.geometry("1180x820")
        self.minsize(980, 700)
        self.configure(bg=APP_BG)
        self._tooltips: list[ToolTip] = []
        self._job_rows: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._batch_sources: list[Path] = []
        self._clip_rows: dict[str, dict[str, Any]] = {}
        self._clip_quality: dict[str, str] = {}
        self._copy_clips: set[str] = set()
        self._force_deinterlace: set[str] = set()
        self._skip_deinterlace: set[str] = set()
        self._progress_highwater: dict[str, dict[str, float]] = {}
        self._busy = 0
        self._jobs_after_id: str | None = None
        self._watch_after_id: str | None = None
        self._watch_state = self._load_watch_state()
        self._icon_photo: tk.PhotoImage | None = None
        self._native_icons: tuple[int, int] | None = None
        self._convert_canvas: tk.Canvas | None = None
        self._build_style()
        self._load_icon()
        self._build_menu()
        self._build_header()
        self._build_tabs()
        self._setup_workflow()
        self._preset_defaults = self._preset_data()
        self._watch_busy = False
        self._watch_stop = threading.Event()
        if self._watch_state.get("source"):
            self.batch_source_var.set(self._watch_state["source"])
            self.batch_output_var.set(self._watch_state.get("output", ""))
            self.batch_recursive_var.set(bool(self._watch_state.get("recursive")))
            self.watch_stability_var.set(int(self._watch_state.get("stable_seconds") or 120))
        self.watch_button.configure(text="Stop watched batch" if self._watch_state.get("active") else "Start watched batch")
        self._set_submission_state()
        self.bind_all("<MouseWheel>", self._route_mousewheel, add="+")
        self.bind_all("<F1>", lambda _event: self._show_quick_help(), add="+")
        self.after(100, self._apply_windows_icons)
        self.after(400, self._refresh_jobs)
        self.after(1500, self._watch_tick)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=APP_BG, foreground=INK)
        style.configure("TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid")
        style.configure("Header.TFrame", background=HEADER)
        style.configure("TLabel", background=APP_BG, foreground=INK)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), background=HEADER, foreground="white")
        style.configure("HeaderMuted.TLabel", background=HEADER, foreground=HEADER_MUTED)
        style.configure("Queue.TLabel", font=("Segoe UI Semibold", 10), background=HEADER, foreground="white")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11), background=PANEL, foreground=ACCENT_DARK)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), foreground="white", background=ACCENT)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9cbfbd")])
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 9), font=("Segoe UI Semibold", 10), background=TAB_BG)
        style.map("TNotebook.Tab", background=[("selected", PANEL), ("active", ACCENT_LIGHT)], foreground=[("selected", ACCENT_DARK)])
        style.configure("Treeview", rowheight=27, background=PANEL, fieldbackground=PANEL)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#dcebed", foreground=INK)
        style.configure("Horizontal.TProgressbar", troughcolor="#dfe7ef", background=ACCENT)
        style.configure("Video.Horizontal.TProgressbar", troughcolor="#dfe7ef", background=ACCENT)
        style.configure("Audio.Horizontal.TProgressbar", troughcolor="#dfe7ef", background="#3b78c8")
        style.configure("Mux.Horizontal.TProgressbar", troughcolor="#dfe7ef", background="#d98b2b")

    def _load_icon(self) -> None:
        png = ROOT / "assets" / "BD2HEVC.png"
        if png.is_file():
            try:
                self._icon_photo = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_photo)
            except tk.TclError:
                self._icon_photo = None
        try:
            self.iconbitmap(default=str(ROOT / "assets" / "BD2HEVC.ico"))
        except tk.TclError:
            pass

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Choose source backup…", command=self._browse_source)
        file_menu.add_command(label="Open selected output", command=self._open_selected_output)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._close_window)
        menu.add_cascade(label="File", menu=file_menu)
        queue_menu = tk.Menu(menu, tearoff=False)
        queue_menu.add_command(label="Pause after current job", command=lambda: self._set_queue_paused(True))
        queue_menu.add_command(label="Resume queue", command=lambda: self._set_queue_paused(False))
        queue_menu.add_command(label="Cancel all…", command=self._cancel_all)
        menu.add_cascade(label="Queue", menu=queue_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Quick start (F1)", command=self._show_quick_help)
        help_menu.add_command(label="Check dependencies", command=self._run_diagnostics)
        help_menu.add_command(label="Open documentation", command=lambda: self._open_path(ROOT / "README.md", create=False))
        help_menu.add_separator()
        help_menu.add_command(label="About BD2HEVC", command=self._show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menu)

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 16, 24, 15))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="BD2HEVC", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text="Compact, menu-preserving Blu-ray backups with playlist-aware quality and concurrent encode/audio/mux work  •  F1 for help",
            style="HeaderMuted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        self.queue_state = ttk.Label(header, text="Queue: checking…", style="Queue.TLabel")
        self.queue_state.pack(side="right")
        tk.Frame(self, bg=ACCENT, height=4).pack(fill="x")

    def _build_tabs(self) -> None:
        self.busy_label = ttk.Label(self, text="Ready", foreground=MUTED, padding=(20, 4), wraplength=1000)
        self.busy_label.pack(side="bottom", fill="x")
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.convert_tab = ttk.Frame(self.notebook, padding=12)
        self.clips_tab = ttk.Frame(self.notebook, padding=12)
        self.batch_tab = ttk.Frame(self.notebook, padding=12)
        self.jobs_tab = ttk.Frame(self.notebook, padding=12)
        self.tools_tab = ttk.Frame(self.notebook, padding=12)
        for frame, label in (
            (self.convert_tab, "Convert"), (self.clips_tab, "Clips & playlists"),
            (self.batch_tab, "Batch queue"), (self.jobs_tab, "Jobs & progress"),
            (self.tools_tab, "Presets & tools"),
        ):
            self.notebook.add(frame, text=label)
        self._build_convert_tab()
        self._build_clips_tab()
        self._build_batch_tab()
        self._build_jobs_tab()
        self._build_tools_tab()

    @staticmethod
    def _panel(parent: tk.Misc, title: str, description: str = "") -> ttk.Frame:
        panel = ttk.Frame(parent, style="Card.TFrame", padding=16)
        ttk.Label(panel, text=title, style="Section.TLabel").pack(anchor="w")
        if description:
            ttk.Label(panel, text=description, style="Muted.TLabel", wraplength=1040).pack(anchor="w", pady=(2, 12))
        return panel

    def _attach_help(self, widget: tk.Misc, text: str) -> tk.Misc:
        self._tooltips.append(ToolTip(widget, text))
        return widget

    def _button(self, parent: tk.Misc, *, tooltip: str, **kwargs: Any) -> ttk.Button:
        button = ttk.Button(parent, **kwargs)
        command = getattr(kwargs.get("command"), "__name__", "")
        self._buttons.setdefault(command, []).append(button)
        return self._attach_help(button, tooltip)

    def _path_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, command: Callable[[], None], *, custom: bool = False) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, style="Panel.TLabel", width=15).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._button(row, text="Browse…", command=command, tooltip=FIELD_HELP.get(label, f"Choose {label.lower()}.")).pack(side="left")

    def _combo(self, parent: ttk.Frame, row: int, column: int, label: str, variable: tk.Variable, values: tuple[Any, ...], *, editable: bool = False) -> None:
        help_text = FIELD_HELP.get(label, f"Choose the {label.lower()} setting.")
        lbl = ttk.Label(parent, text=label, style="Panel.TLabel")
        lbl.grid(row=row, column=column, sticky="w", pady=5, padx=(0, 8))
        box = ttk.Combobox(parent, textvariable=variable, values=values, state="normal" if editable else "readonly", width=28)
        self._fields[label] = box
        box.bind("<MouseWheel>", self._safe_wheel)
        box.grid(row=row, column=column + 1, sticky="ew", pady=5, padx=(0, 24))
        # TCombobox changes its selection on MouseWheel before the app-wide
        # canvas binding runs.  Consume the widget event and scroll the form
        # so settings cannot change merely because the pointer crossed them.
        box.bind("<MouseWheel>", self._safe_wheel)
        self._attach_help(lbl, help_text)
        self._attach_help(box, help_text)
        parent.columnconfigure(column + 1, weight=1)

    def _spin(self, parent: ttk.Frame, row: int, column: int, label: str, variable: tk.Variable, low: float, high: float, step: float) -> None:
        help_text = FIELD_HELP.get(label, f"Adjust the {label.lower()} setting.")
        lbl = ttk.Label(parent, text=label, style="Panel.TLabel")
        lbl.grid(row=row, column=column, sticky="w", pady=5, padx=(0, 8))
        spin = ttk.Spinbox(parent, textvariable=variable, from_=low, to=high, increment=step, width=10)
        self._fields[label] = spin
        spin.bind("<MouseWheel>", self._safe_wheel)
        spin.grid(row=row, column=column + 1, sticky="w", pady=5, padx=(0, 24))
        self._attach_help(lbl, help_text)
        self._attach_help(spin, help_text)

    def _build_convert_tab(self) -> None:
        actions = ttk.Frame(self.convert_tab, padding=(2, 8))
        actions.pack(side="bottom", fill="x")
        canvas = tk.Canvas(self.convert_tab, bg=APP_BG, highlightthickness=0)
        self._convert_canvas = canvas
        scrollbar = ttk.Scrollbar(self.convert_tab, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        source = self._panel(body, "Source and destination", "Paste or browse a decrypted BDMV source and output path. Check the final destination below.")
        source.pack(fill="x", pady=(0, 10))
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.output_format_var = tk.StringVar(value="folder")
        self.add_tags_var = tk.BooleanVar(value=True)
        self.job_name_var = tk.StringVar()
        self._path_row(source, "Source backup", self.source_var, self._browse_source)
        self._path_row(source, "Output path", self.output_var, self._browse_output, custom=True)
        self._destination_controls(source)
        tags = ttk.Checkbutton(source, text="Add (BD) (UHD converted) tags to output names", variable=self.add_tags_var, command=self._regenerate_output)
        tags.pack(anchor="w", padx=(124, 0), pady=(3, 0))
        format_row = ttk.Frame(source, style="Panel.TFrame")
        format_row.pack(fill="x", pady=(8, 0))
        format_label = ttk.Label(format_row, text="Output format", style="Panel.TLabel", width=15)
        format_label.pack(side="left")
        format_box = ttk.Combobox(format_row, textvariable=self.output_format_var, values=("folder", "iso"), state="readonly", width=22)
        format_box.pack(side="left")
        format_box.bind("<MouseWheel>", self._safe_wheel)
        format_box.bind("<<ComboboxSelected>>", self._output_format_changed)
        self._attach_help(format_label, FIELD_HELP["Output format"])
        self._attach_help(format_box, FIELD_HELP["Output format"])
        meta = ttk.Frame(source, style="Panel.TFrame")
        meta.pack(fill="x", pady=(8, 0))
        ttk.Label(meta, text="Job name", style="Panel.TLabel", width=15).pack(side="left")
        ttk.Entry(meta, textvariable=self.job_name_var).pack(side="left", fill="x", expand=True)

        video = self._panel(body, "Video and playlist quality", "Lower CQ = higher quality and larger files. Main feature includes shared clips and alternate cuts.")
        video.pack(fill="x", pady=10)
        self.preset_var = tk.StringVar(value="Custom")
        ttk.Label(video, textvariable=self.preset_var, style="Muted.TLabel").pack(anchor="w")
        grid = ttk.Frame(video, style="Panel.TFrame")
        grid.pack(fill="x")
        self.general_quality_var = tk.StringVar(value="balanced")
        self.override_mode_var = tk.StringVar(value="None")
        self.override_quality_var = tk.StringVar(value="cq:18")
        self.top_n_var = tk.IntVar(value=3)
        self.encoder_var = tk.StringVar(value="hevc_nvenc")
        self.bit_depth_var = tk.IntVar(value=8)
        self.encode_ahead_var = tk.BooleanVar(value=True)
        self.encode_depth_var = tk.IntVar(value=3)
        self.deinterlace_var = tk.StringVar(value=DEFAULT_DEINTERLACE_MODE)
        self.deinterlace_filter_var = tk.StringVar(value="bwdif")
        self.source_ratio_var = tk.StringVar()
        self.codec_ratios_var = tk.StringVar()
        self.min_bitrate_var = tk.StringVar(value="2M")
        self.max_bitrate_var = tk.StringVar(value="80M")
        self.maxrate_multiplier_var = tk.DoubleVar(value=1.55)
        self.bufsize_multiplier_var = tk.DoubleVar(value=2.0)
        self.compact_min_duration_var = tk.StringVar(value="10s")
        self.force_encode_var = tk.BooleanVar(value=False)
        self._combo(grid, 0, 0, "General quality", self.general_quality_var, QUALITY_VALUES, editable=True)
        self._combo(grid, 0, 2, "Feature override", self.override_mode_var, ("None", "Main feature", "Top N clips"))
        self._combo(grid, 1, 0, "Override quality", self.override_quality_var, QUALITY_VALUES, editable=True)
        self._spin(grid, 1, 2, "Top N clips", self.top_n_var, 1, 99, 1)
        self._combo(grid, 2, 0, "Encoder", self.encoder_var, HEVC_ENCODERS)
        self._combo(grid, 2, 2, "Bit depth", self.bit_depth_var, (8, 10))
        self._combo(grid, 3, 0, "Deinterlace", self.deinterlace_var, ("off", "auto", "force"))
        self._combo(grid, 3, 2, "Deinterlace filter", self.deinterlace_filter_var, ("bwdif", "yadif"))
        pipeline = ttk.Frame(video, style="Panel.TFrame")
        pipeline.pack(fill="x", pady=(8, 0))
        ahead = ttk.Checkbutton(pipeline, text="Overlap hardware encoding with audio and muxing", variable=self.encode_ahead_var)
        ahead.pack(side="left")
        self._attach_help(ahead, FIELD_HELP["Encode ahead"])
        ttk.Label(pipeline, text="Queue depth", style="Panel.TLabel").pack(side="left", padx=(20, 6))
        depth = ttk.Spinbox(pipeline, textvariable=self.encode_depth_var, from_=1, to=12, width=6)
        depth.pack(side="left")
        depth.bind("<MouseWheel>", self._safe_wheel)
        self._fields["Queue depth"] = depth

        advanced = self._panel(body, "Advanced bitrate controls", "Defaults are safe for normal use. These controls mirror the CLI for unusual sources and repeatable experiments.")
        advanced.pack(fill="x", pady=10)
        grid = ttk.Frame(advanced, style="Panel.TFrame")
        grid.pack(fill="x")
        self._combo(grid, 0, 0, "Source ratio", self.source_ratio_var, ("", "0.45", "0.50", "0.55", "0.60", "0.65"), editable=True)
        self._combo(grid, 0, 2, "Codec ratios", self.codec_ratios_var, ("", "h264=0.55,vc1=0.45,mpeg2video=0.30"), editable=True)
        self._combo(grid, 1, 0, "Minimum bitrate", self.min_bitrate_var, ("1M", "1.5M", "2M", "2.5M", "3M"), editable=True)
        self._combo(grid, 1, 2, "Maximum bitrate", self.max_bitrate_var, ("40M", "60M", "80M"), editable=True)
        self._spin(grid, 2, 0, "Max-rate multiplier", self.maxrate_multiplier_var, 1.0, 3.0, 0.05)
        self._spin(grid, 2, 2, "Buffer multiplier", self.bufsize_multiplier_var, 1.0, 4.0, 0.1)
        self._combo(grid, 3, 0, "CQ minimum length", self.compact_min_duration_var, ("10s", "30s", "5m", "15m"), editable=True)
        force_encode = ttk.Checkbutton(grid, text="Force eligible HEVC source clips to reencode too", variable=self.force_encode_var)
        force_encode.grid(row=3, column=2, columnspan=2, sticky="w", pady=5)
        self._attach_help(force_encode, "Normally HEVC source video is copied. Enable this only when you deliberately want every eligible long clip reencoded.")

        audio = self._panel(body, "Audio", "Compact audio is an independent pipeline lane and can overlap hardware video encoding.")
        audio.pack(fill="x", pady=10)
        grid = ttk.Frame(audio, style="Panel.TFrame")
        grid.pack(fill="x")
        self.audio_mode_var = tk.StringVar(value="passthrough")
        self.stereo_rate_var = tk.StringVar(value="256k")
        self.mono_rate_var = tk.StringVar(value="128k")
        self._combo(grid, 0, 0, "Audio", self.audio_mode_var, ("passthrough", "compact-stereo"))
        self._combo(grid, 0, 2, "Stereo AC-3", self.stereo_rate_var, ("192k", "224k", "256k", "320k", "384k"), editable=True)
        self._combo(grid, 1, 0, "Mono AC-3", self.mono_rate_var, ("96k", "112k", "128k", "160k", "192k"), editable=True)

        output = self._panel(body, "Output, compatibility, and validation", "Library mode is the normal VLC-oriented full-disc output; physical-disc mode is intentionally stricter.")
        output.pack(fill="x", pady=10)
        grid = ttk.Frame(output, style="Panel.TFrame")
        grid.pack(fill="x")
        self.profile_var = tk.StringVar(value="library")
        self.disc_size_var = tk.StringVar(value="")
        self.disc_margin_var = tk.DoubleVar(value=0.98)
        self.vlc_compat_var = tk.StringVar(value="auto")
        self.makemkv_var = tk.StringVar(value="off")
        self.decode_sample_var = tk.DoubleVar(value=30.0)
        self.fast_bitrate_var = tk.BooleanVar(value=False)
        self.keep_padding_var = tk.BooleanVar(value=False)
        self.patch_navigation_var = tk.BooleanVar(value=True)
        self.bdj_patches_var = tk.BooleanVar(value=True)
        self.force_var = tk.BooleanVar(value=False)
        self._combo(grid, 0, 0, "Output profile", self.profile_var, ("library", "disc"))
        self._combo(grid, 0, 2, "Target disc size", self.disc_size_var, ("", "bd25", "bd50", "bd66", "bd100"), editable=True)
        self._spin(grid, 1, 0, "Disc margin", self.disc_margin_var, 0.80, 1.0, 0.01)
        self._combo(grid, 1, 2, "VLC compatibility", self.vlc_compat_var, ("auto", "off"))
        self._combo(grid, 2, 0, "MakeMKV scan", self.makemkv_var, ("off", "optional", "required"))
        self._spin(grid, 2, 2, "Validation", self.decode_sample_var, 0, 300, 5)
        checks = ttk.Frame(output, style="Panel.TFrame")
        checks.pack(fill="x", pady=(8, 0))
        for text, variable, help_text in (
            ("Fast bitrate estimate", self.fast_bitrate_var, "Skip exact packet/padding measurement for faster planning."),
            ("Keep source padding", self.keep_padding_var, "Count coded filler bytes when deriving VBR targets."),
            ("Patch navigation", self.patch_navigation_var, "Update CLPI/MPLS video descriptors and packet maps for HEVC replacements."),
            ("Known BD-J fixes", self.bdj_patches_var, "Apply release-tested compatibility fixes only when their signatures match."),
            ("Replace existing output", self.force_var, "Delete an existing output folder before converting. Use carefully."),
        ):
            widget = ttk.Checkbutton(checks, text=text, variable=variable)
            widget.pack(side="left", padx=(0, 18))
            self._attach_help(widget, help_text)

        self._button(actions, text="Inspect clips", command=self._inspect_clips, tooltip="Scan physical clips and MPLS playlists, then open the Clips & playlists tab.").pack(side="right", padx=(8, 0))
        self._button(actions, text="Plan only", command=self._plan_only, tooltip="Run the complete read-only planner and save a report without starting conversion.").pack(side="right", padx=(8, 0))
        self._button(actions, text="Add to queue", style="Accent.TButton", command=self._queue_one, tooltip="Add this full-disc conversion to the one-job-at-a-time background queue.").pack(side="right")

    def _build_clips_tab(self) -> None:
        panel = self._panel(self.clips_tab, "Physical clips and feature playlists", "Inspect first, then optionally override individual clips. Main feature rows include every clip used by detected alternate cuts.")
        panel.pack(fill="both", expand=True)
        self.feature_label = ttk.Label(panel, text="Choose a source and click Inspect clips.", style="Muted.TLabel", wraplength=1040)
        self.feature_label.pack(anchor="w", fill="x", pady=(0, 8))
        columns = ("role", "clip", "duration", "codec", "field", "quality")
        self.clips_tree = ttk.Treeview(panel, columns=columns, show="headings", height=15, selectmode="extended")
        widths = {"role": 115, "clip": 105, "duration": 85, "codec": 100, "field": 90, "quality": 250}
        for column in columns:
            self.clips_tree.heading(column, text=column.title())
            self.clips_tree.column(column, width=widths[column], stretch=column == "quality")
        pack_scrollable(self.clips_tree, fill="both", expand=True)
        bar = ttk.Frame(panel, style="Panel.TFrame")
        bar.pack(fill="x", pady=(10, 0))
        self._button(bar, text="Set quality…", command=self._set_clip_quality, tooltip="Give every selected physical clip a specific quality.").pack(side="left")
        self._button(bar, text="Reset overrides", command=self._clear_clip_override, tooltip="Reset quality, copy and deinterlace overrides for the selected clips.").pack(side="left", padx=6)
        self._button(bar, text="Copy untouched", command=self._copy_selected_clips, tooltip="Keep selected source video codecs instead of converting them to HEVC.").pack(side="left")
        self._button(bar, text="Force deinterlace", command=lambda: self._set_clip_deinterlace(True), tooltip="Force BWDIF/YADIF on selected clips.").pack(side="right")
        self._button(bar, text="Never deinterlace", command=lambda: self._set_clip_deinterlace(False), tooltip="Protect selected clips from automatic or forced deinterlacing.").pack(side="right", padx=6)

    def _build_batch_tab(self) -> None:
        panel = self._panel(self._scrollable_tab(self.batch_tab), "Queue or watch Blu-ray folder backups", "Watch discovery runs while this GUI is open; queued jobs continue after closing. Output is a parent folder (blank for Queue all = beside each source).")
        panel.pack(fill="both", expand=True)
        self.batch_source_var = tk.StringVar()
        self.batch_output_var = tk.StringVar()
        self.batch_recursive_var = tk.BooleanVar(value=False)
        self.watch_stability_var = tk.IntVar(value=120)
        self._path_row(panel, "Source folder", self.batch_source_var, self._browse_batch_source)
        self._path_row(panel, "Output folder", self.batch_output_var, self._browse_batch_output)
        options = ttk.Frame(panel, style="Panel.TFrame")
        options.pack(fill="x", pady=7)
        ttk.Checkbutton(options, text="Include subfolders", variable=self.batch_recursive_var).pack(side="left")
        ttk.Label(options, text="Stable for", style="Panel.TLabel").pack(side="left", padx=(18, 6))
        ttk.Spinbox(options, textvariable=self.watch_stability_var, from_=30, to=1800, increment=30, width=7).pack(side="left")
        ttk.Label(options, text="seconds", style="Panel.TLabel").pack(side="left", padx=(4, 0))
        self._button(options, text="Scan folder", command=self._scan_batch, tooltip="Find BDMV backups and preview output names without queuing them.").pack(side="right")
        columns = ("status", "source", "output")
        self.batch_tree = ttk.Treeview(panel, columns=columns, show="headings", height=5)
        for column, width in (("status", 110), ("source", 500), ("output", 390)):
            self.batch_tree.heading(column, text=column.title())
            self.batch_tree.column(column, width=width, stretch=column in {"source", "output"})
        pack_scrollable(self.batch_tree, fill="both", expand=True, pady=(5, 10))
        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x")
        self.batch_summary = ttk.Label(footer, text="No folder scanned", style="Muted.TLabel")
        self.batch_summary.pack(side="left")
        self.watch_button = self._button(footer, text="Start watched batch", style="Accent.TButton", command=self._toggle_watch, tooltip="Persistently monitor this folder while the GUI is available. Only stable, complete BDMV backups are queued.")
        self.watch_button.pack(side="right")
        self._button(footer, text="Reset watch history", command=self._reset_watch_history, tooltip="Forget which source fingerprints the watched batch has already handled. Existing outputs are still never overwritten.").pack(side="right", padx=6)
        self._button(footer, text="Queue all shown (0)", command=self._queue_batch, tooltip="Queue the currently discovered complete backups using the Convert settings.").pack(side="right")

    def _build_jobs_tab(self) -> None:
        panel = self._panel(self.jobs_tab, "Conversion jobs", "One disc runs at a time. Within that disc, hardware video, compact audio, muxing, navigation repair, and validation may overlap.")
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel, style="Panel.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        self._button(toolbar, text="Refresh", command=self._refresh_jobs, tooltip="Reload job and progress state now.").pack(side="left")
        self.pause_button = self._button(toolbar, text="Pause after current", command=self._toggle_queue, tooltip="Prevent later jobs from starting; the active disc continues.")
        self.pause_button.pack(side="left", padx=6)
        self._button(toolbar, text="Cancel selected", command=self._cancel_selected, tooltip="Immediately stop the selected queued or running process tree.").pack(side="left")
        self._button(toolbar, text="Cancel all…", command=self._cancel_all, tooltip="Pause the queue and cancel every active or waiting job.").pack(side="left", padx=6)
        self._button(toolbar, text="Remove selected", command=self._remove_selected, tooltip="Remove an inactive history entry without deleting its output.").pack(side="left")
        self._button(toolbar, text="Play output", command=self._play_selected, tooltip="Open the selected converted backup in a clean VLC Blu-ray menu session.").pack(side="right")
        self._button(toolbar, text="Open output", command=self._open_selected_output, tooltip="Open the selected output folder in File Explorer.").pack(side="right", padx=6)
        columns = ("status", "progress", "source", "quality", "audio", "output")
        self.jobs_tree = ttk.Treeview(panel, columns=columns, show="headings", height=5)
        widths = {"status": 82, "progress": 82, "source": 220, "quality": 110, "audio": 120, "output": 360}
        for column in columns:
            self.jobs_tree.heading(column, text=column.title())
            self.jobs_tree.column(column, width=widths[column], stretch=column in {"source", "output"})
        jobs_holder = pack_scrollable(self.jobs_tree, fill="both", expand=True)
        self.jobs_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_job_detail())
        for state, color in {"running": "#e2f5f2", "queued": "#fff4dd", "paused": "#edf3f6", "completed": "#e8f5e5", "failed": "#fde8e6", "canceled": "#f0edf2"}.items():
            self.jobs_tree.tag_configure(state, background=color)
        progress = ttk.Frame(panel, style="Panel.TFrame")
        progress.pack(fill="x", pady=(12, 0))
        self.overall_label = ttk.Label(progress, text="Select a job", style="Panel.TLabel")
        self.overall_label.grid(row=0, column=0, sticky="w")
        self.overall_progress = ttk.Progressbar(progress, maximum=100)
        self.overall_progress.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self.lane_bars: dict[str, ttk.Progressbar] = {}
        self.lane_labels: dict[str, ttk.Label] = {}
        for index, lane in enumerate(("video", "audio", "mux"), start=1):
            label = ttk.Label(progress, text=f"{lane.title()}: waiting", style="Muted.TLabel")
            label.grid(row=index, column=0, sticky="w", pady=(5, 0))
            bar = ttk.Progressbar(progress, maximum=100, style=f"{lane.title()}.Horizontal.TProgressbar")
            bar.grid(row=index, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))
            self.lane_labels[lane] = label
            self.lane_bars[lane] = bar
        progress.columnconfigure(1, weight=1)
        self.job_detail = tk.Text(panel, height=6, wrap="word", relief="flat", bg="#f7f9fc", fg=INK, font=("Consolas", 9))
        details_holder = pack_scrollable(self.job_detail, fill="x", pady=(10, 0))
        self.job_detail.configure(state="disabled")
        detail_actions = ttk.Frame(panel, style="Panel.TFrame")
        detail_actions.pack(fill="x", pady=(4, 0))
        self._button(detail_actions, text="Open log", command=self._open_selected_log, tooltip="Open the selected job's diagnostic log.").pack(side="left")
        self._button(detail_actions, text="Copy details", command=self._copy_job_details, tooltip="Copy full paths, settings and error details.").pack(side="left", padx=6)
        # Reserve the footer before allowing the table to use the remaining height.
        for footer in (detail_actions, details_holder, progress):
            footer.pack_configure(side="bottom", before=jobs_holder)

    def _build_tools_tab(self) -> None:
        presets = self._panel(self.tools_tab, "Reusable presets", "Save the complete Convert policy or load a JSON preset shared with the CLI.")
        presets.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.preset_tree = ttk.Treeview(presets, columns=("name", "source", "description"), show="headings")
        self.preset_tree.heading("name", text="Name")
        self.preset_tree.column("name", width=180)
        self.preset_tree.heading("source", text="Kind")
        self.preset_tree.heading("description", text="Description")
        self.preset_tree.column("source", width=100)
        self.preset_tree.column("description", width=380)
        pack_scrollable(self.preset_tree, fill="both", expand=True)
        row = ttk.Frame(presets, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        self._button(row, text="Load selected", command=self._load_selected_preset, tooltip="Apply the selected preset to the Convert controls.").pack(side="left")
        self._button(row, text="Save current as…", command=self._save_preset, tooltip="Save current quality, audio, encoder, pipeline, and output settings.").pack(side="left", padx=6)
        self._button(row, text="Remove selected", command=self._remove_preset, tooltip="Delete a personal preset. Bundled/example presets are protected.").pack(side="left")
        tools = self._panel(self.tools_tab, "Tools and completed outputs", "Dependency checks and common CLI maintenance workflows are available without opening a command prompt.")
        tools.pack(side="left", fill="y", padx=(6, 0))
        self._button(tools, text="Check dependencies", style="Accent.TButton", command=self._run_diagnostics, tooltip="Check FFmpeg, encoders, tsMuxeR, MakeMKV, and VLC.").pack(fill="x")
        self._button(tools, text="Validate selected output", command=self._validate_selected, tooltip="Run structural and decode validation on the selected job output.").pack(fill="x", pady=(8, 0))
        self._button(tools, text="Repair selected output…", command=self._repair_selected, tooltip="Use current repair rules on the selected output after confirmation.").pack(fill="x", pady=(6, 0))
        self._button(tools, text="Create support bundle", command=self._diagnose_selected, tooltip="Create a redacted diagnostic zip without including media data.").pack(fill="x", pady=(6, 0))
        ttk.Separator(tools).pack(fill="x", pady=14)
        self._button(tools, text="Open reports", command=lambda: self._open_path(REPORT_ROOT), tooltip="Open job logs, plans, reports, and diagnostics.").pack(fill="x")
        self._button(tools, text="Open project folder", command=lambda: self._open_path(ROOT), tooltip="Open the BD2HEVC installation folder.").pack(fill="x", pady=6)
        ttk.Label(tools, text="Converted Blu-ray folders play through ordinary VLC/libbluray; unlike HEVC DVD backups, no private VLC build is required.", style="Muted.TLabel", wraplength=290).pack(anchor="w", pady=(14, 0))
        self._refresh_presets()


    def _apply_windows_icons(self) -> None:
        if sys.platform != "win32":
            return
        icon = str(ROOT / "assets" / "BD2HEVC.ico")
        if not Path(icon).is_file():
            return
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            big = int(user32.LoadImageW(None, icon, 1, 64, 64, 0x10) or 0)
            small = int(user32.LoadImageW(None, icon, 1, 32, 32, 0x10) or 0)
            wrapper = int(user32.GetParent(self.winfo_id()) or self.winfo_id())
            if big and small:
                user32.SendMessageW(wrapper, 0x0080, 1, big)
                user32.SendMessageW(wrapper, 0x0080, 0, small)
                self._native_icons = (big, small)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _browse_source(self) -> None:
        value = filedialog.askdirectory(title="Choose Blu-ray backup folder", parent=self)
        if value:
            self.source_var.set(str(source_path(value, "BD")))
            self._update_path_preview()

    def _source_path_changed(self, *_args: Any) -> None:
        self._paths_changed()

    def _browse_output(self) -> None:
        if self.destination_mode_var.get() != "Full output path":
            value = filedialog.askdirectory(title="Choose parent folder for converted backups", parent=self)
        elif self._output_format() == "iso":
            value = filedialog.asksaveasfilename(title="Name converted ISO", parent=self,
                defaultextension=".iso", filetypes=(("ISO image", "*.iso"),))
        else:
            value = filedialog.askdirectory(title="Choose the final output folder (its name receives enabled tags)", parent=self, mustexist=False)
        if value:
            self.output_var.set(value)
            self._update_path_preview()

    def _output_format_changed(self, *_args: Any) -> None:
        self._paths_changed()

    def _browse_batch_source(self) -> None:
        if value := filedialog.askdirectory(title="Choose folder containing Blu-ray backups", parent=self):
            self.batch_source_var.set(value)

    def _browse_batch_output(self) -> None:
        if value := filedialog.askdirectory(title="Choose converted-backup output folder", parent=self):
            self.batch_output_var.set(value)

    def _regenerate_output(self, *_args: Any) -> None:
        self._paths_changed()

    def _conversion_flags(self, *, include_clip_overrides: bool = True) -> list[str]:
        flags = [
            "--quality", self.general_quality_var.get().strip(),
            "--encoder", self.encoder_var.get(),
            "--hevc-bit-depth", str(self.bit_depth_var.get()),
            "--encode-ahead-depth", str(self.encode_depth_var.get()),
            "--deinterlace", self.deinterlace_var.get(),
            "--deinterlace-filter", self.deinterlace_filter_var.get(),
            "--audio-mode", self.audio_mode_var.get(),
            "--stereo-audio-bitrate", self.stereo_rate_var.get(),
            "--mono-audio-bitrate", self.mono_rate_var.get(),
            "--uhd-profile", self.profile_var.get(),
            "--output-format", self.output_format_var.get(),
            "--target-disc-margin", str(self.disc_margin_var.get()),
            "--decode-sample", str(self.decode_sample_var.get()),
            "--vlc-compat", self.vlc_compat_var.get(),
            "--min-video-bitrate", self.min_bitrate_var.get().strip(),
            "--max-video-bitrate", self.max_bitrate_var.get().strip(),
            "--maxrate-multiplier", str(self.maxrate_multiplier_var.get()),
            "--bufsize-multiplier", str(self.bufsize_multiplier_var.get()),
            "--compact-cq-min-duration", self.compact_min_duration_var.get().strip(),
        ]
        if self.source_ratio_var.get().strip():
            flags += ["--hevc-bitrate-factor", self.source_ratio_var.get().strip()]
        for ratio in self.codec_ratios_var.get().replace(";", ",").split(","):
            if ratio.strip():
                flags += ["--codec-source-ratio", ratio.strip()]
        if self.force_encode_var.get():
            flags.append("--force-encode")
        mode = self.override_mode_var.get()
        if mode == "Main feature":
            flags += ["--main-title-quality", self.override_quality_var.get().strip()]
        elif mode == "Top N clips":
            flags += ["--top-n-quality", str(self.top_n_var.get()), self.override_quality_var.get().strip()]
        if not self.encode_ahead_var.get():
            flags.append("--no-encode-ahead")
        if self.disc_size_var.get().strip():
            flags += ["--target-disc-size", self.disc_size_var.get().strip()]
        if self.fast_bitrate_var.get():
            flags.append("--fast-bitrate")
        if self.keep_padding_var.get():
            flags.append("--keep-source-padding")
        if not self.patch_navigation_var.get():
            flags.append("--no-patch-navigation")
        if not self.bdj_patches_var.get():
            flags.append("--no-bdj-compatibility-patches")
        if self.force_var.get():
            flags.append("--force")
        makemkv = self.makemkv_var.get()
        if makemkv == "optional":
            flags.append("--makemkv")
        elif makemkv == "required":
            flags.append("--require-makemkv")
        else:
            flags.append("--no-makemkv")
        if include_clip_overrides:
            for clip, quality in sorted(self._clip_quality.items()):
                flags += ["--clip-quality", clip, quality]
            if self._copy_clips:
                flags += ["--copy-clips", *sorted(self._copy_clips)]
            if self._force_deinterlace:
                flags += ["--deinterlace-clips", *sorted(self._force_deinterlace)]
            if self._skip_deinterlace:
                flags += ["--no-deinterlace-clips", *sorted(self._skip_deinterlace)]
        return flags

    def _clip_scan_flags(self) -> list[str]:
        flags = ["--quality", self.general_quality_var.get().strip(), "--deinterlace", self.deinterlace_var.get(), "--deinterlace-filter", self.deinterlace_filter_var.get(), "--no-makemkv"]
        mode = self.override_mode_var.get()
        if mode == "Main feature":
            flags += ["--main-title-quality", self.override_quality_var.get().strip()]
        elif mode == "Top N clips":
            flags += ["--top-n-quality", str(self.top_n_var.get()), self.override_quality_var.get().strip()]
        for clip, quality in sorted(self._clip_quality.items()):
            flags += ["--clip-quality", clip, quality]
        if self._copy_clips:
            flags += ["--copy-clips", *sorted(self._copy_clips)]
        if self._force_deinterlace:
            flags += ["--deinterlace-clips", *sorted(self._force_deinterlace)]
        if self._skip_deinterlace:
            flags += ["--no-deinterlace-clips", *sorted(self._skip_deinterlace)]
        return flags

    def _queue_source(self, source: Path, output: Path, name: str, flags: list[str]) -> dict[str, Any]:
        try:
            args = build_parser().parse_args(["start", str(source), str(output), "--name", name, *flags])
        except SystemExit as exc:
            raise ToolError("Invalid conversion settings. Check the selected encoder, quality and numeric fields.") from exc
        return enqueue_conversion_job(args, announce=False)

    def _validate_paths(self) -> tuple[Path, Path]:
        self._update_path_preview()
        source, output = self._resolved_paths()
        if not backup_looks_complete(source):
            raise ToolError(f"The source is not a complete BDMV folder backup: {source}")
        return source, output

    def _queue_one(self) -> None:
        try:
            source, output = self._validate_paths()
            flags = self._conversion_flags()
        except Exception as exc:
            messagebox.showerror("Invalid conversion", str(exc), parent=self)
            return
        if output.exists() and not self.force_var.get():
            messagebox.showerror("Output exists", "Choose another output or explicitly enable Replace existing output.", parent=self)
            return
        name = self.job_name_var.get().strip() or source.name
        self._background("Adding conversion to queue…", lambda: self._queue_one_work(source, output, name, flags), key="submission")

    def _queue_one_work(self, source: Path, output: Path, name: str, flags: list[str]) -> str:
        job = self._queue_source(source, output, name, flags)
        self._post(lambda: self.notebook.select(self.jobs_tab))
        return f"Queued {job['id']}"

    def _plan_only(self) -> None:
        try:
            source, output = self._validate_paths()
            flags = self._conversion_flags()
        except Exception as exc:
            messagebox.showerror("Invalid conversion", str(exc), parent=self)
            return
        def work() -> str:
            report = REPORT_ROOT / "gui-plans" / f"{safe_name(source.name)}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(ROOT / "bd2hevc.py"), "auto", str(source), str(output), *flags, "--dry-run", "--no-progress", "--report", str(report)]
            completed = subprocess.run(cmd, cwd=ROOT, env=refreshed_env(), text=True, capture_output=True, **hidden_process_kwargs())
            if completed.returncode:
                raise ToolError((completed.stderr or completed.stdout).strip())
            return f"Plan passed and was saved to:\n{report}"
        self._background("Planning disc…", work, show_result=True, key="submission")

    def _inspect_clips(self) -> None:
        try:
            self._update_path_preview()
            source = source_path(self.source_var.get(), "BD")
            flags = self._clip_scan_flags()
            if not backup_looks_complete(source):
                raise ToolError("Choose a complete BDMV backup first")
        except Exception as exc:
            messagebox.showerror("Inspect clips", str(exc), parent=self)
            return
        def work() -> str:
            cmd = [sys.executable, str(ROOT / "bd2hevc.py"), "clips", str(source), "--json", *flags]
            completed = subprocess.run(cmd, cwd=ROOT, env=refreshed_env(), text=True, capture_output=True, **hidden_process_kwargs())
            if completed.returncode:
                raise ToolError((completed.stderr or completed.stdout).strip())
            payload = json.loads(completed.stdout)
            self._post(lambda: self._render_clips(payload) if self._previous_source == source else None)
            return f"Inspected {len(payload.get('clips') or [])} physical clips"
        self._background("Inspecting clips and playlists…", work)

    def _render_clips(self, payload: dict[str, Any]) -> None:
        self._clip_rows.clear()
        self.clips_tree.delete(*self.clips_tree.get_children())
        feature = payload.get("main_feature") or {}
        feature_ids = set(feature.get("clip_ids") or [])
        playlists = feature.get("playlists") or []
        if feature:
            cuts = ", ".join(f"{row.get('playlist')} ({format_duration(row.get('duration'))})" for row in playlists)
            branch = "seamless branching detected" if feature.get("seamless_branching") else "single feature playlist"
            self.feature_label.configure(text=f"Main feature: {branch}; primary {feature.get('primary_playlist')}. Related playlists: {cuts}")
        else:
            self.feature_label.configure(text="No confident feature playlist was detected; Main feature falls back to the longest eligible physical clip.")
        for row in payload.get("clips") or []:
            clip = str(row.get("clip") or "")
            role = "Main feature" if Path(clip).stem in feature_ids else "Extra / menu"
            quality = self._clip_quality.get(clip) or ("copy" if clip in self._copy_clips else str(row.get("planned_quality") or ""))
            if clip in self._force_deinterlace:
                quality += "; force deint"
            elif clip in self._skip_deinterlace:
                quality += "; no deint"
            self._clip_rows[clip] = row
            self.clips_tree.insert("", "end", iid=clip, values=(role, clip, row.get("duration_text"), row.get("codec"), row.get("field_order") or "-", quality))
        self.notebook.select(self.clips_tab)

    def _selected_clips(self) -> list[str]:
        return [str(item) for item in self.clips_tree.selection()]

    def _set_clip_quality(self) -> None:
        clips = self._selected_clips()
        if not clips:
            return
        quality = simpledialog.askstring("Clip quality", "Quality (for example cq:18, balanced, or source-ratio:0.60):", initialvalue="cq:18", parent=self)
        if not quality:
            return
        for clip in clips:
            self._clip_quality[clip] = quality.strip()
            self._copy_clips.discard(clip)
        self._inspect_clips()

    def _clear_clip_override(self) -> None:
        for clip in self._selected_clips():
            self._clip_quality.pop(clip, None)
            self._copy_clips.discard(clip)
            self._force_deinterlace.discard(clip)
            self._skip_deinterlace.discard(clip)
        self._inspect_clips()

    def _copy_selected_clips(self) -> None:
        for clip in self._selected_clips():
            self._copy_clips.add(clip)
            self._clip_quality.pop(clip, None)
        self._inspect_clips()

    def _set_clip_deinterlace(self, enabled: bool) -> None:
        for clip in self._selected_clips():
            (self._force_deinterlace if enabled else self._skip_deinterlace).add(clip)
            (self._skip_deinterlace if enabled else self._force_deinterlace).discard(clip)
        self._inspect_clips()

    def _scan_batch(self) -> None:
        if self._watch_state.get("active"):
            messagebox.showinfo("Scan folder", "The active watch owns this preview. Stop it before scanning a different batch.", parent=self)
            return
        try:
            source = self._batch_source()
            recursive = bool(self.batch_recursive_var.get())
        except Exception as exc:
            messagebox.showerror("Scan folder", str(exc), parent=self)
            return
        def show(sources):
            if source != self._batch_source() or recursive != bool(self.batch_recursive_var.get()):
                return
            self._batch_sources = sources
            self._render_batch()
        def work() -> str:
            sources = discover_disc_sources(source, recursive=recursive)
            self._post(lambda: show(sources))
            return f"Found {len(sources)} Blu-ray backup(s)"
        self._background("Scanning backup folders…", work)

    def _render_batch(self, watch_state=None) -> None:
        try:
            watch_state = watch_state or (self._watch_state if self._watch_state.get("active") else None)
            if watch_state:
                pairs = [(source, generated_output(source, Path(watch_state["output"]),
                    add_tags=bool(watch_state.get("add_tags", True)), output_format=watch_state.get("output_format", "folder")))
                    for source in self._batch_sources]
            else:
                pairs = self._batch_paths()
            for source, output in pairs:
                status = "output exists" if output.exists() else ("complete" if backup_looks_complete(source) else "incomplete")
                update_row(self.batch_tree, str(source), (status, str(source), str(output)))
            prune_rows(self.batch_tree, {str(source) for source in self._batch_sources})
            self.batch_summary.configure(text=(f"Watch uses saved settings: {len(pairs)} backups → {watch_state['output']}" if watch_state else f"{len(pairs)} backup(s) shown; existing outputs are skipped"))
            self._set_batch_count(len(pairs))
        except (OSError, ValueError) as exc:
            prune_rows(self.batch_tree, set())
            self.batch_summary.configure(text=str(exc))
            self._set_batch_count(0)

    def _queue_batch(self) -> None:
        if self._watch_state.get("active"):
            messagebox.showinfo("Batch queue", "The active watch queues this preview with its saved settings. Stop it to queue a batch with the current Convert settings.", parent=self)
            return
        if not self._batch_sources:
            messagebox.showinfo("Batch queue", "Scan a source folder first.", parent=self)
            return
        try:
            pairs = self._batch_paths()
            flags = self._conversion_flags(include_clip_overrides=False)
            flags = [flag for flag in flags if flag != "--force"]
        except Exception as exc:
            messagebox.showerror("Batch queue", str(exc), parent=self)
            return
        def work() -> str:
            queued, issues = 0, []
            for source, output in pairs:
                try:
                    if output.exists():
                        issues.append(f"{source.name}: skipped; output exists ({output})")
                    elif not backup_looks_complete(source):
                        issues.append(f"{source.name}: skipped; backup is incomplete")
                    else:
                        self._queue_source(source, output, source.name, flags)
                        queued += 1
                except Exception as exc:
                    issues.append(f"{source.name}: {exc}")
            self._post(lambda: self.notebook.select(self.jobs_tab))
            return f"Queued {queued} of {len(pairs)} backups." + ("\n\n" + "\n".join(issues) if issues else "")
        self._background("Queuing Blu-ray backups…", work, show_result=True, key="submission")

    def _load_watch_state(self) -> dict[str, Any]:
        try:
            return json.loads(WATCH_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active": False, "seen": {}, "observed": {}}

    def _save_watch_state(self) -> None:
        WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
        WATCH_STATE.write_text(json.dumps(self._watch_state, indent=2) + "\n", encoding="utf-8")

    def _toggle_watch(self) -> None:
        if self._watch_state.get("active"):
            self._watch_stop.set()
            self._watch_state["active"] = False
            self._save_watch_state()
            self.watch_button.configure(text="Start watched batch")
            self._set_submission_state()
            self.batch_summary.configure(text="Watch stopped; any admission already in progress and queued jobs may finish")
            return
        if self._watch_busy:
            messagebox.showinfo("Watched batch", "The previous scan is finishing. Start again when it completes.", parent=self)
            return
        try:
            source = self._batch_source()
            raw = path_text(self.batch_output_var.get())
            if not raw:
                raise ValueError("Choose an output parent folder for the watched batch.")
            output = Path(raw).expanduser().resolve()
            if output.suffix.lower() == ".iso" or (output.exists() and not output.is_dir()):
                raise ValueError("The watched output destination must be a folder.")
            if output == source or source in output.parents or output in source.parents:
                raise ValueError("Keep watched source and output folders separate, not nested.")
            flags = [flag for flag in self._conversion_flags(include_clip_overrides=False) if flag != "--force"]
            stable = int(self.watch_stability_var.get())
            if stable < 30:
                raise ValueError("Watch stability must be at least 30 seconds.")
        except Exception as exc:
            messagebox.showerror("Watched batch", str(exc), parent=self)
            return
        same_destination = self._watch_state.get("source") == str(source) and self._watch_state.get("output") == str(output)
        self._watch_state = {
            "active": True, "source": str(source), "output": str(output),
            "recursive": bool(self.batch_recursive_var.get()), "stable_seconds": stable,
            "flags": flags, "add_tags": bool(self.add_tags_var.get()), "output_format": self.output_format_var.get(),
            "seen": self._watch_state.get("seen", {}) if same_destination else {}, "observed": {},
        }
        self._watch_stop.clear()
        self._save_watch_state()
        self.watch_button.configure(text="Stop watched batch")
        self._set_submission_state()
        self._watch_tick()

    def _reset_watch_history(self) -> None:
        if self._watch_busy:
            messagebox.showinfo("Watch history", "Stop the watch and wait for its scan to finish before resetting history.", parent=self)
            return
        self._watch_state["seen"] = {}
        self._watch_state["observed"] = {}
        self._save_watch_state()
        self.batch_summary.configure(text="Watched-batch history reset; existing outputs remain protected")

    def _watch_tick(self) -> None:
        if self._watch_after_id is not None:
            self.after_cancel(self._watch_after_id)
        if self._watch_state.get("active") and not self._watch_busy:
            self._watch_busy = True
            state = copy.deepcopy(self._watch_state)
            def work():
                error = None
                try:
                    sources, summary = self._watch_scan_once(state)
                except Exception as exc:
                    sources, summary, error = [], f"Watch attention: {exc}", exc
                def finished():
                    self._watch_busy = False
                    # Stop can be clicked during a scan; never reactivate it.
                    active = self._watch_state.get("active", False)
                    state["active"] = active
                    self._watch_state = state
                    self._save_watch_state()
                    if active:
                        self._batch_sources = sources
                        self._render_batch(watch_state=state)
                        self.batch_summary.configure(text=summary)
                self._post(finished)
            threading.Thread(target=work, name="bd2hevc-watch-scan", daemon=True).start()
        self._watch_after_id = self.after(15000, self._watch_tick)

    def _watch_scan_once(self, state) -> tuple[list[Path], str]:
        sources = discover_disc_sources(Path(state["source"]), recursive=bool(state.get("recursive")))
        now = time.time()
        observed = state.setdefault("observed", {})
        seen = state.setdefault("seen", {})
        queued = 0
        waiting = 0
        issues = []
        for source in sources:
            if self._watch_stop.is_set():
                break
            key = str(source).casefold()
            fingerprint = source_fingerprint(source)
            signature = f"{fingerprint['files']}:{fingerprint['bytes']}:{fingerprint['latest_ns']}"
            output = generated_output(source, Path(state["output"]), add_tags=bool(state.get("add_tags", True)), output_format=str(state.get("output_format") or "folder"))
            if output.exists():
                seen[key] = signature
                continue
            previous = observed.get(key) or {}
            if previous.get("signature") != signature:
                observed[key] = {"signature": signature, "stable_since": now}
                waiting += 1
                continue
            if now - float(previous.get("stable_since") or now) < int(state.get("stable_seconds") or 120):
                waiting += 1
                continue
            if not backup_looks_complete(source):
                waiting += 1
                continue
            if seen.get(key) == signature:
                continue
            if not queue_is_paused() and not self._watch_stop.is_set():
                try:
                    flags = watch_conversion_flags(list(state.get("flags") or []))
                    self._queue_source(source, output, source.name, flags)
                    seen[key] = signature
                    queued += 1
                except Exception as exc:
                    issues.append(f"{source.name}: {exc}")
        summary = f"Watch uses saved settings: {len(sources)} found · {queued} newly queued · {waiting} settling"
        if issues:
            summary += " · Attention: " + "; ".join(issues)
        return sources, summary

    def _selected_job_id(self) -> str | None:
        selected = self.jobs_tree.selection() if hasattr(self, "jobs_tree") else ()
        return str(selected[0]) if selected else None

    def _refresh_jobs(self) -> None:
        if self._jobs_after_id is not None:
            try:
                self.after_cancel(self._jobs_after_id)
            except tk.TclError:
                pass
            self._jobs_after_id = None
        selected = self._selected_job_id()
        self._job_rows.clear()
        states: list[str] = []
        for path in known_job_files():
            job = try_load_job(path)
            if not job:
                continue
            status = job_runtime_status(job)
            job["job_file"] = str(path)
            snap = job_progress_snapshot(job)
            high = self._progress_highwater.setdefault(str(job.get("id")), {"overall": 0.0, "video": 0.0, "audio": 0.0, "mux": 0.0})
            for key in high:
                high[key] = max(high[key], float(snap[key]))
                snap[key] = high[key]
            job["gui_progress"] = snap
            identifier = str(job.get("id"))
            self._job_rows[identifier] = (path, job)
            states.append(status)
            update_row(self.jobs_tree, identifier,
                values=(status, f"{snap['overall']:.1f}%", Path(str(job.get("source"))).name, command_option(job, "--quality", "balanced"), command_option(job, "--audio-mode", "passthrough"), job.get("output")),
                tags=(status,),
            )
        prune_rows(self.jobs_tree, self._job_rows)
        paused = queue_is_paused()
        pause_reason = queue_pause_reason()
        pause_text = f"paused: {pause_reason}" if paused and pause_reason else ("paused" if paused else "active")
        self.queue_state.configure(text=f"Queue: {pause_text} · {states.count('running')} running · {sum(s in {'queued', 'paused'} for s in states)} queued")
        self.pause_button.configure(text="Resume queue" if paused else "Pause after current")
        self._show_job_detail()
        self._jobs_after_id = self.after(1500, self._refresh_jobs)

    def _show_job_detail(self) -> None:
        identifier = self._selected_job_id()
        if not identifier or identifier not in self._job_rows:
            self._clear_job_detail()
            return
        _path, job = self._job_rows[identifier]
        snap = job.get("gui_progress") or job_progress_snapshot(job)
        self.overall_progress["value"] = snap["overall"]
        self.overall_label.configure(text=f"Overall {snap['overall']:.1f}% · {job_runtime_status(job)} · {snap['stage']}")
        details = snap.get("detail") or {}
        lane_text = {
            "video": f"{snap['video']:.1f}%" + (f" · {details.get('encode_file')} {details.get('encode_speed') or ''}" if details.get("encode_file") else ""),
            "audio": f"{snap['audio']:.1f}%" + (f" · {details.get('audio_file')} {details.get('audio_speed') or ''}" if details.get("audio_file") else ""),
            "mux": f"{snap['mux']:.1f}%" + (f" · {details.get('mux_file')}" if details.get("mux_file") else ""),
        }
        for lane in ("video", "audio", "mux"):
            self.lane_bars[lane]["value"] = snap[lane]
            self.lane_labels[lane].configure(text=f"{lane.title()}: {lane_text[lane]}")
        text = (
            f"Job: {identifier}\nSource: {job.get('source')}\nOutput: {job.get('output')}\n"
            f"Video: {command_option(job, '--quality', 'balanced')} · {command_option(job, '--encoder', 'hevc_nvenc')} · Main{command_option(job, '--hevc-bit-depth', '8')}\n"
            f"Audio: {command_option(job, '--audio-mode', 'passthrough')} · Profile: {command_option(job, '--uhd-profile', 'library')}\nLog: {job.get('log')}"
        )
        error = job_error(job, job_runtime_status(job))
        set_detail(self.job_detail, (f"Error: {error}\n" if error else "") + text)
        self._update_job_actions(job, job_runtime_status(job))

    def _toggle_queue(self) -> None:
        self._set_queue_paused(not queue_is_paused())

    def _set_queue_paused(self, paused: bool) -> None:
        (cmd_pause_queue if paused else cmd_resume_queue)(argparse.Namespace(reason=None))
        self._refresh_jobs()

    def _cancel_selected(self) -> None:
        identifier = self._selected_job_id()
        if identifier and messagebox.askyesno("Cancel job", f"Immediately cancel {identifier}?", parent=self):
            self._background("Canceling job…", lambda: (cmd_cancel_job(argparse.Namespace(job=identifier, kill=True)), "Cancellation requested")[1])

    def _cancel_all(self) -> None:
        if not messagebox.askyesno("Cancel all", "Pause the queue and immediately cancel every running or waiting BD2HEVC job?", parent=self):
            return
        def work() -> str:
            cmd_pause_queue(argparse.Namespace(reason="Canceled all jobs from the GUI"))
            for path in known_job_files():
                job = try_load_job(path)
                if job and job_runtime_status(job) in {"running", "queued", "paused"}:
                    cmd_cancel_job(argparse.Namespace(job=str(job.get("id")), kill=True))
            return "All active jobs canceled; queue paused. Resume queue also resumes any active watched batch."
        self._background("Canceling all jobs…", work, show_result=True)

    def _remove_selected(self) -> None:
        identifier = self._selected_job_id()
        if identifier and messagebox.askyesno("Remove job", "Remove this inactive history entry? Converted output is retained.", parent=self):
            self._background("Removing job…", lambda: (cmd_remove_job(argparse.Namespace(job=identifier, kill=False)), "Job removed")[1])

    def _selected_output(self) -> Path | None:
        identifier = self._selected_job_id()
        if identifier and identifier in self._job_rows:
            return Path(str(self._job_rows[identifier][1].get("output") or ""))
        return None

    def _open_selected_output(self) -> None:
        if output := self._selected_output():
            self._open_path(output)

    def _play_selected(self) -> None:
        if output := self._selected_output():
            self._run_cli(["play", str(output)], "Opening VLC…", capture=False)

    def _validate_selected(self) -> None:
        if output := self._selected_output():
            self._run_cli(["verify-iso" if output.suffix.lower() == ".iso" else "validate", str(output)], "Validating output…", show_result=True)

    def _repair_selected(self) -> None:
        output = self._selected_output()
        identifier = self._selected_job_id()
        if not output or not identifier or identifier not in self._job_rows:
            return
        if not output.is_dir():
            messagebox.showinfo("Repair output", "Repair requires a converted BDMV folder. ISO repair is not supported; use Validate for an ISO.", parent=self)
            return
        source = self._job_rows[identifier][1].get("source")
        if messagebox.askyesno("Repair output", "Apply current repair rules to this converted backup? Video is not reencoded unless repair requires it.", parent=self):
            self._run_cli(["repair-output", str(source), str(output)], "Repairing output…", show_result=True)

    def _diagnose_selected(self) -> None:
        if output := self._selected_output():
            self._run_cli(["diagnose", str(output)], "Creating support bundle…", show_result=True)

    def _run_cli(self, args: list[str], label: str, *, capture: bool = True, show_result: bool = False) -> None:
        def work() -> str:
            completed = subprocess.run([sys.executable, str(ROOT / "bd2hevc.py"), *args], cwd=ROOT, env=refreshed_env(), text=True, capture_output=capture, **hidden_process_kwargs())
            if completed.returncode:
                raise ToolError(((completed.stderr or "") + (completed.stdout or "")).strip() or f"Command failed with {completed.returncode}")
            return ((completed.stdout or "") + (completed.stderr or "")).strip() or "Command completed"
        self._background(label, work, show_result=show_result)



    def _refresh_presets(self) -> None:
        rows = available_presets()
        for row in rows:
            update_row(self.preset_tree, str(row["name"]), (row["name"], row.get("source"), row.get("description") or ""))
        prune_rows(self.preset_tree, {str(row["name"]) for row in rows})

    def _clear_source_overrides(self) -> None:
        for values in (self._clip_quality, self._copy_clips, self._force_deinterlace, self._skip_deinterlace, self._clip_rows):
            values.clear()
        if hasattr(self, "clips_tree"):
            prune_rows(self.clips_tree, set())
            self.feature_label.configure(text="Source changed; inspect clips to set overrides for this disc.")

    def _preset_fields(self):
        return {
            "quality": self.general_quality_var, "encoder": self.encoder_var, "hevc_bit_depth": self.bit_depth_var,
            "deinterlace": self.deinterlace_var, "deinterlace_filter": self.deinterlace_filter_var,
            "audio_mode": self.audio_mode_var, "stereo_audio_bitrate": self.stereo_rate_var, "mono_audio_bitrate": self.mono_rate_var,
            "encode_ahead": self.encode_ahead_var, "encode_ahead_depth": self.encode_depth_var,
            "uhd_profile": self.profile_var, "output_format": self.output_format_var, "add_tags": self.add_tags_var,
            "target_disc_size": self.disc_size_var, "target_disc_margin": self.disc_margin_var,
            "vlc_compat": self.vlc_compat_var, "makemkv_scan": self.makemkv_var, "decode_sample": self.decode_sample_var,
            "fast_bitrate": self.fast_bitrate_var, "keep_source_padding": self.keep_padding_var,
            "patch_navigation": self.patch_navigation_var, "bdj_compatibility_patches": self.bdj_patches_var,
            "force": self.force_var, "force_encode": self.force_encode_var,
            "hevc_bitrate_factor": self.source_ratio_var, "min_video_bitrate": self.min_bitrate_var,
            "max_video_bitrate": self.max_bitrate_var, "maxrate_multiplier": self.maxrate_multiplier_var,
            "bufsize_multiplier": self.bufsize_multiplier_var, "anime_cq_min_duration": self.compact_min_duration_var,
        }

    def _preset_data(self):
        data = {key: variable.get() for key, variable in self._preset_fields().items()}
        ratios = [v.strip() for v in self.codec_ratios_var.get().replace(";", ",").split(",") if v.strip()]
        data["codec_source_ratio"] = codec_ratio_cli_values(ratios) if ratios else []
        if not data["hevc_bitrate_factor"]:
            data.pop("hevc_bitrate_factor")
        if self.override_mode_var.get() == "Main feature":
            data["main_title_quality"] = self.override_quality_var.get()
        elif self.override_mode_var.get() == "Top N clips":
            data["top_n_quality"] = [self.top_n_var.get(), self.override_quality_var.get()]
        return data

    def _load_selected_preset(self) -> None:
        selected = self.preset_tree.selection()
        if not selected:
            return
        try:
            _path, data = load_named_preset(str(selected[0]))
            self._apply_preset_data(data)
        except Exception as exc:
            messagebox.showerror("Load preset", str(exc), parent=self)
            return
        self.preset_var.set(str(selected[0]))
        self.notebook.select(self.convert_tab)

    def _apply_preset_data(self, data):
        resolved = {**self._preset_defaults, **data}
        resolved["quality"] = data.get("quality") or data.get("bitrate_mode") or data.get("mode") or "balanced"
        if resolved["quality"] in {"compact-cq", "episode-compact", "anime-cq"} and "compact_cq_value" in data:
            resolved["quality"] = f"cq:{int(data['compact_cq_value'])}"
        resolved["hevc_bitrate_factor"] = data.get("hevc_bitrate_factor", data.get("factor", ""))
        aliases = {"min_bps": "min_video_bitrate", "max_bps": "max_video_bitrate", "compact_cq_min_duration": "anime_cq_min_duration"}
        for alias, key in aliases.items():
            if alias in data and key not in data:
                resolved[key] = data[alias]
        ratios = next((data[key] for key in ("codec_source_ratios", "codec_source_ratio", "codec_hevc_bitrate_factors", "codec_factors") if key in data), [])
        ratio_text = ",".join(codec_ratio_cli_values(ratios)) if ratios else ""
        self._loading_preset = True
        try:
            for key, variable in self._preset_fields().items():
                variable.set(resolved.get(key, ""))
            self.codec_ratios_var.set(ratio_text)
            self.override_mode_var.set("None")
            self.override_quality_var.set("cq:18")
            self.top_n_var.set(3)
            if data.get("main_title_quality"):
                self.override_mode_var.set("Main feature")
                self.override_quality_var.set(data["main_title_quality"])
            elif data.get("top_n_quality"):
                count, quality = data["top_n_quality"]
                self.override_mode_var.set("Top N clips")
                self.top_n_var.set(int(count))
                self.override_quality_var.set(quality)
            self._clear_source_overrides()
        finally:
            self._loading_preset = False
        self._update_control_states()
        self._update_path_preview()

    def _save_preset(self) -> None:
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self)
        if not name:
            return
        try:
            path = named_preset_path(name)
            if path.exists() and not messagebox.askyesno("Replace preset", f"Replace {path.stem}?", parent=self):
                return
            data = self._preset_data()
            data["description"] = "Saved from the BD2HEVC GUI (disc-specific clip overrides excluded)"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Save preset", str(exc), parent=self)
            return
        self.preset_var.set(path.stem)
        self._refresh_presets()

    def _remove_preset(self) -> None:
        selected = self.preset_tree.selection()
        if not selected:
            return
        row = next((item for item in available_presets() if item["name"] == selected[0]), None)
        if not row or row.get("source") != "user":
            messagebox.showinfo("Remove preset", "Only personal presets can be removed.", parent=self)
            return
        if messagebox.askyesno("Remove preset", f"Delete {selected[0]}?", parent=self):
            Path(row["path"]).unlink()
            self._refresh_presets()

    def _run_diagnostics(self) -> None:
        self._run_cli(["tools"], "Checking dependencies…", show_result=True)

    def _show_quick_help(self) -> None:
        messagebox.showinfo(
            "BD2HEVC quick start",
            "1. Choose a decrypted Blu-ray folder backup and output folder.\n\n"
            "2. Choose general video quality. Main feature applies its override to every clip in the main MPLS playlist and related seamless-branched cuts; Top N clips is better for episode discs.\n\n"
            "3. Passthrough preserves audio. Compact-stereo uses an independent AC-3 lane that can overlap NVENC and muxing.\n\n"
            "4. Inspect clips for per-clip controls, Plan only for a read-only preflight, or Add to queue. Only one disc job runs at once.\n\n"
            "5. Batch queue can wait for MakeMKV folder backups to stop changing before admitting them. Jobs & progress shows separate video, audio, and mux/validation lanes.\n\n"
            "Closing this GUI stops Blu-ray folder discovery; already queued conversions continue. Reopening restores an active watch. Pause/Cancel all pauses discovery until Resume queue.\n\nHover over controls for details. Source backups are never changed.",
            parent=self,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About BD2HEVC",
            f"BD2HEVC {VERSION}\n\nCreates compact HEVC full-disc Blu-ray folder backups while preserving menus, playlists, extras, subtitles, audio selection, and BD-J resources.\n\n"
            "Outputs are library-oriented and not certified UHD-BD physical media. Converted video requires a HEVC-capable player such as VLC/libbluray.",
            parent=self,
        )






def launch_gui() -> int:
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    app = BD2HEVCApp()
    app.mainloop()
    return 0
