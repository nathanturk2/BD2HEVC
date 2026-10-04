"""Small, media-independent helpers shared by the desktop workflows.

Kept in each distribution so the two applications can be installed separately.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .tools import hidden_process_kwargs as hidden_subprocess_kwargs


def path_text(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def source_path(value: str, kind: str) -> Path:
    value = path_text(value)
    if not value:
        raise ValueError("Choose a source backup first.")
    source = Path(value).expanduser().resolve()
    return source.parent if kind == "BD" and source.name.upper() == "BDMV" else source


def destination_mode(value: str, mode: str) -> str:
    """Infer once per edit; the GUI retains this decision if output is created."""
    if mode != "Auto detect":
        return mode
    value = path_text(value)
    if not value or value.endswith(("/", "\\")):
        return "Destination folder"
    entered = Path(value).expanduser()
    if entered.is_dir():
        return "Destination folder"
    if entered.suffix.lower() == ".iso" or (not entered.exists() and entered.parent.is_dir()):
        return "Full output path"
    return "Destination folder"


def destination_path(source: Path, value: str, *, kind: str, tags: bool,
                     output_format: str = "iso", mode: str = "Auto detect", dvd_output: str = "dvd-hevc") -> Path:
    """Resolve preview and submission alike, without creating directories.

    An ISO pasted into the folder field is unambiguously a complete filename.
    A missing final component with an existing parent is a custom backup name.
    A trailing slash or explicit folder mode requests a parent destination instead.
    Only the final name receives tags; the selected parent is never renamed.
    """
    value = path_text(value)
    entered = Path(value).expanduser().resolve() if value else None
    final = entered if entered and destination_mode(value, mode) == "Full output path" else None
    if final:
        name = final.name[:-4] if final.name.lower().endswith(".iso") else final.name
        parent = final.parent
    else:
        parent = entered or source.parent
        name = source.stem if kind == "DVD" else source.name
        if kind == "DVD":
            name = re.sub(r"\s*\((?:DVD|HEVC|UHD-BD)\)", "", name, flags=re.I)
            name = re.sub(r"\s+-\s+converted$", "", name, flags=re.I).strip() or "DVD"
    if not name:
        raise ValueError("Enter an output name, or choose Destination folder.")
    if tags:
        if kind == 'DVD' and dvd_output == 'uhd-bd':
            name = re.sub(r"\s*\(HEVC\)", "", name, flags=re.I)
        for tag in (("BD", "UHD converted") if kind == "BD" else ("DVD", "UHD-BD" if dvd_output == 'uhd-bd' else "HEVC")):
            if f"({tag})".casefold() not in name.casefold():
                name += f" ({tag})"
    elif not final and kind == "DVD":
        name = re.sub(r"\s*\((?:DVD|HEVC)\)", "", name, flags=re.I)
        if not name.lower().endswith(" - converted"):
            name += " - converted"
    target = parent / (name + (".iso" if output_format == "iso" else ""))
    if not final and target == source:
        target = parent / (name + " HEVC" + (".iso" if output_format == "iso" else ""))
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("The output must be separate from the source backup, not inside or above it.")
    if parent.exists() and not parent.is_dir():
        raise ValueError(f"The destination parent is not a folder: {parent}")
    return target


def update_row(tree, identifier, values, tags=()):
    if tree.exists(identifier):
        # Tcl returns numeric values as strings; compare their display values.
        if tuple(map(str, tree.item(identifier, "values"))) != tuple(map(str, values)) or tuple(tree.item(identifier, "tags")) != tuple(tags):
            tree.item(identifier, values=values, tags=tags)
    else:
        tree.insert("", "end", iid=identifier, values=values, tags=tags)


def prune_rows(tree, identifiers):
    for item in tree.get_children():
        if item not in identifiers:
            tree.delete(item)


def pack_scrollable(widget, **options):
    holder = ttk.Frame(widget.master)
    holder.pack(**options)
    widget.pack(in_=holder, side="left", fill="both", expand=True)
    scroll = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
    scroll.pack(side="right", fill="y")
    widget.configure(yscrollcommand=scroll.set)
    if isinstance(widget, ttk.Treeview):
        # Pack below the tree while retaining a single holder in its parent.
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=widget.xview)
        horizontal.pack(side="bottom", fill="x", before=widget)
        widget.configure(xscrollcommand=horizontal.set)
    # The widget was created before its holder and remains a sibling in Tcl.
    # Raise it above that newer frame, otherwise Windows paints an empty panel.
    widget.tk.call("raise", widget._w)
    return holder


def set_detail(widget, text):
    if widget.get("1.0", "end-1c") == text:
        return
    position = widget.yview()[0]
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")
    widget.yview_moveto(position)


def job_error(job, status):
    error = job.get("error") or job.get("failure_reason") or job.get("partial_output_cleanup_error")
    if error:
        return str(error)
    if str(status).startswith("failed"):
        log = Path(str(job.get("log") or ""))
        try:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8192))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            failures = [line.strip() for line in lines if re.search(r"error|failed|exception|not enough|no space", line, re.I)]
            if failures:
                return failures[-1][:600]
        except OSError:
            pass
        return f"Conversion failed (exit code {job.get('returncode', 'unknown')}). Open the log for details."
    return ""


class WorkflowUI:
    """Common path feedback, operation lifecycle and selection safeguards."""
    def _init_workflow(self, kind):
        self._disc_kind = kind
        self._fields = {}
        self._buttons = {}
        self._operations = set()
        self._callbacks = queue.Queue()
        self._closing = False
        self._path_after = None
        self._previous_source = None
        self._destination_decision = None
        self._generated_meta = {}
        self._loading_preset = False
        self._tab_canvases = {}

    def _scrollable_tab(self, tab):
        canvas = tk.Canvas(tab, highlightthickness=0, bg=tab.winfo_toplevel().cget("bg"))
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        self._tab_canvases[str(tab)] = canvas
        return body

    def _route_mousewheel(self, event):
        selected = self.notebook.select()
        canvas = self._convert_canvas if selected == str(self.convert_tab) else self._tab_canvases.get(selected)
        if canvas is None or isinstance(event.widget, (ttk.Treeview, tk.Text)):
            return None
        ancestor = event.widget
        while ancestor is not None and str(ancestor) != selected:
            ancestor = ancestor.master
        if ancestor is None:
            return None
        delta = int(getattr(event, "delta", 0))
        if delta:
            canvas.yview_scroll((-1 if delta > 0 else 1) * max(1, abs(delta) // 120) * 3, "units")
            return "break"
        return None

    def _destination_controls(self, parent):
        self.destination_mode_var = tk.StringVar(value="Auto detect")
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="Treat output as", style="Panel.TLabel", width=15).pack(side="left")
        box = ttk.Combobox(row, textvariable=self.destination_mode_var,
                           values=("Auto detect", "Destination folder", "Full output path"), state="readonly", width=23)
        box.pack(side="left")
        box.bind("<MouseWheel>", self._safe_wheel)
        ttk.Label(row, text="Existing folder = parent. Missing final name = backup name.", style="Muted.TLabel").pack(side="left", padx=8)
        self._attach_help(box, "Auto: an existing folder receives a source-named backup. If only the final path component is missing, use it as the backup name. Add a trailing slash or choose Destination folder to create a new parent folder. Blank means beside the source.")
        self.destination_preview_var = tk.StringVar(value="Choose a source to preview the final destination.")
        preview = ttk.Entry(parent, textvariable=self.destination_preview_var, state="readonly")
        preview.pack(fill="x", pady=(4, 0))
        self._attach_help(preview, "Final destination used by Plan and Add to queue. Select and copy, or scroll horizontally to read the whole path.")

    def _setup_workflow(self):
        for variable in (self.output_var, self.destination_mode_var):
            variable.trace_add("write", self._reset_destination_decision)
        for variable in (self.source_var, self.output_var, self.destination_mode_var, self._tags_variable()):
            variable.trace_add("write", self._paths_changed)
        if self._disc_kind == "BD":
            self.output_format_var.trace_add("write", self._paths_changed)
        for variable in (self.batch_source_var, self.batch_recursive_var):
            variable.trace_add("write", self._invalidate_batch)
        self.batch_output_var.trace_add("write", self._paths_changed)
        self._policy_variables = {name: var for name, var in vars(self).items()
                                  if name.endswith("_var") and isinstance(var, tk.Variable)
                                  and name not in {"source_var", "output_var", "destination_mode_var", "destination_preview_var",
                                                   "job_name_var", "name_var", "label_var", "preset_var"}
                                  and not name.startswith(("batch_", "watch_"))}
        for variable in self._policy_variables.values():
            variable.trace_add("write", self._settings_changed)
        self.protocol("WM_DELETE_WINDOW", self._close_window)
        self.after(50, self._drain_callbacks)
        self._update_path_preview()
        self._update_control_states()
        self._update_job_actions(None, "")
        self._set_submission_state()

    def _tags_variable(self):
        return self.add_tags_var if self._disc_kind == "BD" else self.filename_tags_var

    def _output_format(self):
        return self.output_format_var.get() if self._disc_kind == "BD" else "iso"

    def _resolved_paths(self):
        source = source_path(self.source_var.get(), self._disc_kind)
        if self._destination_decision is None:
            self._destination_decision = destination_mode(self.output_var.get(), self.destination_mode_var.get())
        return source, destination_path(source, self.output_var.get(), kind=self._disc_kind,
            tags=bool(self._tags_variable().get()), output_format=self._output_format(), mode=self._destination_decision,
            dvd_output=self._dvd_output() if hasattr(self,'_dvd_output') else 'dvd-hevc')

    def _reset_destination_decision(self, *_args):
        self._destination_decision = None

    def _paths_changed(self, *_args):
        if self._path_after:
            self.after_cancel(self._path_after)
        self._path_after = self.after(300, self._update_path_preview)

    def _update_path_preview(self):
        if self._path_after:
            self.after_cancel(self._path_after)
        self._path_after = None
        try:
            source = source_path(self.source_var.get(), self._disc_kind)
            if source != self._previous_source:
                if self._disc_kind == "BD":
                    self._clear_source_overrides()
                self._previous_source = source
            name = source.name if self._disc_kind == "BD" else source.stem
            fields = {"job_name_var": name} if self._disc_kind == "BD" else {"name_var": name, "label_var": f"{name}_HEVC"}
            for field, generated in fields.items():
                var = getattr(self, field)
                if not var.get().strip() or var.get() == self._generated_meta.get(field):
                    var.set(generated)
                    self._generated_meta[field] = generated
            self.destination_preview_var.set(str(self._resolved_paths()[1]))
        except (ValueError, OSError) as exc:
            self.destination_preview_var.set(str(exc))
            if not path_text(self.source_var.get()) and self._disc_kind == "BD":
                self._clear_source_overrides()
                self._previous_source = None
        if self._batch_sources:
            self._render_batch()

    def _invalidate_batch(self, *_args):
        self._batch_sources = []
        if hasattr(self, "batch_tree"):
            prune_rows(self.batch_tree, set())
            self.batch_summary.configure(text="Source changed; scan this folder to preview its backups.")
            self._set_batch_count(0)

    def _batch_paths(self, sources=None):
        # A batch destination is always a parent folder, including when it is blank.
        raw = path_text(self.batch_output_var.get())
        if raw and Path(raw).suffix.lower() == ".iso" and not Path(raw).is_dir():
            raise ValueError("Batch output must be a destination folder, not an ISO filename.")
        return [(source, destination_path(source, raw, kind=self._disc_kind,
                 tags=bool(self._tags_variable().get()), output_format=self._output_format(), mode="Destination folder",
                 dvd_output=self._dvd_output() if hasattr(self,'_dvd_output') else 'dvd-hevc'))
                for source in (self._batch_sources if sources is None else sources)]

    def _batch_source(self):
        raw = path_text(self.batch_source_var.get())
        if not raw or not Path(raw).expanduser().is_dir():
            raise ValueError("Choose an existing batch source folder first.")
        return Path(raw).expanduser().resolve()

    def _set_batch_count(self, count):
        for button in self._buttons.get("_queue_batch", []):
            button.configure(text=f"Queue all shown ({count})")

    def _safe_wheel(self, event):
        self._route_mousewheel(event)
        return "break"

    def _settings_changed(self, *_args):
        if not self._loading_preset and hasattr(self, "preset_var"):
            self.preset_var.set("Custom")
        self._update_control_states()

    def _update_control_states(self):
        def enable(labels, enabled):
            for label in labels:
                widget = self._fields.get(label)
                if widget:
                    widget.state(["!disabled"] if enabled else ["disabled"])
        mode = self.override_mode_var.get()
        enable(("Override quality",), mode != "None")
        enable(("Top N", "Top N clips"), mode.startswith("Top N"))
        compact = self.audio_mode_var.get() == "compact-stereo" or "compact-stereo" in getattr(self, "_language_overrides", {}).values()
        enable(("Stereo AC-3", "Mono AC-3", "Audio workers"), compact)
        if self._disc_kind == "DVD":
            enable(("Target multiplier", "Bitrate control"), self.quality_var.get() == "target-bitrate")
        else:
            enable(("Queue depth",), self.encode_ahead_var.get())
            enable(("Deinterlace filter",), self.deinterlace_var.get() != "off")
            enable(("Target disc size", "Disc margin"), self.profile_var.get() == "disc")

    def _post(self, action):
        self._callbacks.put(action)

    def _drain_callbacks(self):
        while not self._callbacks.empty():
            action = self._callbacks.get_nowait()
            try:
                action()
            except Exception as exc:
                messagebox.showerror(self.title(), str(exc), parent=self)
        if not self._closing:
            self.after(50, self._drain_callbacks)

    def _background(self, label, action, *, show_result=False, key=None):
        key = key or label
        if key in self._operations:
            return
        self._operations.add(key)
        self._busy = len(self._operations)
        self.busy_label.configure(text=label)
        self._set_submission_state()
        def run():
            try:
                result, error = action(), None
            except BaseException as exc:
                result, error = "Operation failed; see error details.", str(exc)
            self._post(lambda: self._operation_done(key, result, error, show_result))
        threading.Thread(target=run, name="gui-operation", daemon=True).start()

    def _operation_done(self, key, result, error, show_result):
        self._operations.discard(key)
        self._busy = len(self._operations)
        self.busy_label.configure(text=result if not self._busy else "Working…")
        self._set_submission_state()
        self._refresh_jobs()
        if error:
            messagebox.showerror(self.title(), error, parent=self)
        elif show_result:
            messagebox.showinfo(self.title(), result, parent=self)

    def _set_submission_state(self):
        for command in ("_queue_one", "_plan_only", "_queue_batch", "_start_watched_batch"):
            for button in self._buttons.get(command, []):
                watching = command == "_queue_batch" and getattr(self, "_watch_state", {}).get("active")
                button.state(["disabled"] if "submission" in self._operations or watching else ["!disabled"])

    def _update_job_actions(self, job, status):
        output = Path(str((job or {}).get("output") or ""))
        usable = bool(job and job.get("output") and output.exists() and status in {"passed", "completed"})
        permissions = {
            "_cancel_selected": status in {"planned", "queued", "running", "paused"},
            "_resume_selected": status in {"planned", "failed", "canceled", "interrupted"},
            "_remove_selected": bool(job) and status not in {"queued", "running", "paused"},
            "_play_selected": usable,
            "_open_selected_output": bool(job and job.get("output") and output.exists()),
            "_open_selected_job_folder": bool(job and job.get("work_root") and Path(job["work_root"]).is_dir()),
            "_validate_selected": usable,
            "_repair_selected": usable and output.is_dir(),
            "_diagnose_selected": bool(job and job.get("output") and output.is_dir()),
            "_open_selected_log": bool(job and job.get("log") and Path(job["log"]).is_file()),
            "_copy_job_details": bool(job),
        }
        for command, enabled in permissions.items():
            for button in self._buttons.get(command, []):
                button.state(["!disabled"] if enabled else ["disabled"])

    def _clear_job_detail(self):
        self.overall_label.configure(text="Select a job")
        self.overall_progress["value"] = 0
        for lane in self.lane_bars:
            self.lane_bars[lane]["value"] = 0
            self.lane_labels[lane].configure(text=f"{lane.title()}: waiting")
        set_detail(self.job_detail, "")
        self._update_job_actions(None, "")

    def _open_selected_log(self):
        identifier = self._selected_job_id()
        if identifier in self._job_rows:
            value = self._job_rows[identifier][1].get("log")
            if value:
                self._open_path(Path(value))

    def _copy_job_details(self):
        self.clipboard_clear()
        self.clipboard_append(self.job_detail.get("1.0", "end-1c"))

    def _open_path(self, path, **_kwargs):
        if not path.exists():
            messagebox.showinfo("Open path", f"This path does not exist yet:\n{path}", parent=self)
            return
        if path.suffix.lower() == ".iso" and path.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(path)], **hidden_subprocess_kwargs())
        else:
            os.startfile(str(path))

    def _close_window(self):
        if self._operations or getattr(self, "_watch_busy", False):
            messagebox.showinfo("Work in progress", "Wait for the current planning, scan or tool operation to finish before closing. Queued conversions run independently.", parent=self)
            return
        self._closing = True
        self.destroy()
