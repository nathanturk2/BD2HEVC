"""Media-free regression checks for the actual Tk handlers and path decisions."""
import argparse
import copy
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from bd2hevc_app import gui, core
from bd2hevc_app.gui_support import destination_path, source_path, update_row, prune_rows


class DestinationTests(unittest.TestCase):
    def test_auto_parent_missing_leaf_quotes_and_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "Movie.v1"
            parent = root / "Converted"
            parent.mkdir()
            for kind, fmt, name in (("BD", "folder", "Movie.v1 (BD) (UHD converted)"),
                                    ("BD", "iso", "Movie.v1 (BD) (UHD converted).iso"),
                                    ("DVD", "iso", "Movie.v1 (DVD) (HEVC).iso")):
                disc = source.with_name(source.name + ".iso") if kind == "DVD" else source
                settings = dict(kind=kind, tags=True, output_format=fmt)
                self.assertEqual(destination_path(disc, f'  "{parent}"  ', **settings), parent / name)
                custom = destination_path(disc, str(parent / "My Cut"), **settings)
                self.assertEqual(custom.parent, parent)
                self.assertTrue(custom.name.startswith("My Cut ("))
                self.assertEqual(destination_path(disc, str(custom), mode="Full output path", **settings), custom)
                new_parent = parent / "New Library"
                self.assertEqual(destination_path(disc, str(new_parent) + "/", **settings), new_parent / name)
                self.assertEqual(destination_path(disc, str(new_parent), mode="Destination folder", **settings), new_parent / name)
                self.assertFalse(new_parent.exists(), "Preview must not create output folders")

    def test_iso_stem_custom_no_tags_and_source_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "Movie.iso"
            output = root / "Custom.iso"
            self.assertEqual(destination_path(source, str(output), kind="DVD", tags=True), root / "Custom (DVD) (HEVC).iso")
            self.assertEqual(destination_path(source, str(output), kind="DVD", tags=False), output)
            with self.assertRaises(ValueError):
                destination_path(source, str(source), kind="DVD", tags=False)
            with self.assertRaises(ValueError):
                source_path(' "" ', "BD")
            self.assertEqual(source_path(str(root / "Movie/BDMV"), "BD"), root / "Movie")


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for target, value in (("_load_watch_state", {"active": False}),):
            patcher = patch.object(gui.BD2HEVCApp, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("known_job_files", []), ("available_presets", [])):
            patcher = patch.object(gui, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.app = gui.BD2HEVCApp()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.app.withdraw()
        self.app.update_idletasks()
        self.addCleanup(self.close_app)

    def close_app(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def source(self, name="Film"):
        disc = self.root / name
        (disc / "BDMV/STREAM").mkdir(parents=True)
        (disc / "BDMV/index.bdmv").write_bytes(b"fixture")
        (disc / "BDMV/MovieObject.bdmv").write_bytes(b"fixture")
        return disc

    def test_typed_source_paste_preview_and_scoped_overrides(self):
        source = self.source()
        # Reproduce character-by-character editing, including pauses while typing.
        for end in range(1, len(str(source)) + 1):
            self.app.source_var.set(str(source)[:end])
            self.app._update_path_preview()
        self.assertEqual(self.app.job_name_var.get(), source.name)
        self.app._clip_quality['00001.m2ts'] = 'cq:18'
        self.app._force_deinterlace.add('00001.m2ts')
        self.app.source_var.set(str(self.root / "Next/BDMV"))
        self.app._update_path_preview()
        self.assertFalse(self.app._clip_quality)
        self.assertFalse(self.app._force_deinterlace)
        self.app.job_name_var.set("My friendly label")
        self.app.source_var.set(str(source))
        self.app.output_var.set(f'"{self.root / "Custom"}"')
        self.app._update_path_preview()
        self.assertEqual(self.app.job_name_var.get(), "My friendly label")
        output = self.app._resolved_paths()[1]
        self.assertEqual(output.name, "Custom (BD) (UHD converted)")
        self.app.add_tags_var.set(False)
        self.assertEqual(self.app._resolved_paths()[1], self.root / "Custom")
        (self.root / "Custom").mkdir()
        self.assertEqual(self.app._resolved_paths()[1], self.root / "Custom", "An output created during work must not turn into a new parent")

    def test_preset_round_trip_and_reset_absent_values(self):
        a = self.app
        a.override_mode_var.set("Main feature")
        a.source_ratio_var.set("0.33")
        a.codec_ratios_var.set("h264=0.40")
        a.encode_depth_var.set(7)
        a.encode_ahead_var.set(False)
        a.output_format_var.set("iso")
        a.add_tags_var.set(False)
        a.force_var.set(True)
        saved = a._preset_data()
        a._apply_preset_data({"quality": "cq:22"})
        self.assertEqual(a.override_mode_var.get(), "None")
        self.assertEqual(a.source_ratio_var.get(), "")
        self.assertEqual(a.codec_ratios_var.get(), "")
        self.assertEqual(a.encode_depth_var.get(), 3)
        self.assertFalse(a.force_var.get())
        a._apply_preset_data(saved)
        self.assertEqual(a._preset_data(), saved)
        self.assertNotIn("--clip-quality", a._conversion_flags(include_clip_overrides=False))
        a._apply_preset_data({"codec_source_ratios": {"h264": 0.5}, "factor": 0.4})
        self.assertEqual(a.codec_ratios_var.get(), "h264=0.5")
        a._apply_preset_data({'mode': 'compact-cq', 'compact_cq_value': 20, 'compact_cq_min_duration': '10m'})
        self.assertEqual(a.general_quality_var.get(), 'cq:20')
        self.assertEqual(a.compact_min_duration_var.get(), '10m')

    def test_queue_and_batch_capture_click_settings(self):
        source = self.source()
        a = self.app
        a.source_var.set(str(source / "BDMV"))
        a.output_var.set(str(self.root / "Custom"))
        actions = []
        with patch.object(a, '_background', side_effect=lambda label, action, **kw: actions.append(action)), patch.object(gui, 'backup_looks_complete', return_value=True):
            a.general_quality_var.set("cq:20")
            a._queue_one()
        a.general_quality_var.set("cq:26")
        with patch.object(a, '_queue_source', return_value={"id": "fixture"}) as enqueue:
            actions[0]()
            flags = enqueue.call_args.args[3]
            self.assertEqual(flags[flags.index('--quality')+1], "cq:20")
        a._batch_sources = [source, self.source('Next')]
        a.batch_output_var.set("")
        a._clip_quality['00001.m2ts'] = 'copy'
        actions.clear()
        with patch.object(a, '_background', side_effect=lambda label, action, **kw: actions.append(action)):
            a._queue_batch()
        a.add_tags_var.set(False)
        with patch.object(a, '_queue_source', side_effect=[RuntimeError("first disc failed"), {"id": "second"}]) as enqueue, patch.object(gui, 'backup_looks_complete', return_value=True):
            result = actions[0]()
        self.assertIn("Queued 1 of 2", result)
        self.assertIn("first disc failed", result)
        self.assertNotIn('--clip-quality', enqueue.call_args.args[3])
        self.assertIn('(BD)', enqueue.call_args.args[1].name)

    def test_submission_guard_worker_callback_and_button_states(self):
        a = self.app
        event = threading.Event()
        self.addCleanup(event.set)
        calls = []
        a._background('Test', lambda: (calls.append(1), event.wait(3), 'Done')[-1], key='submission')
        a._background('Duplicate', lambda: calls.append(2), key='submission')
        self.assertIn('disabled', a._buttons['_queue_one'][0].state())
        event.set()
        deadline = time.monotonic() + 4
        while a._operations and time.monotonic() < deadline:
            a.update()
            time.sleep(.01)
        self.assertEqual(calls, [1])
        self.assertFalse(a._operations)
        self.assertNotIn('disabled', a._buttons['_queue_one'][0].state())
        a._update_job_actions({'output': str(self.root / 'missing.iso')}, 'queued')
        self.assertIn('disabled', a._buttons['_play_selected'][0].state())
        self.assertNotIn('disabled', a._buttons['_cancel_selected'][0].state())
        with patch('os.startfile') as start:
            with patch.object(gui.messagebox, 'showinfo'):
                a._open_path(self.root / 'missing')
            start.assert_not_called()
            self.assertFalse((self.root / 'missing').exists())

    def test_watch_scan_runs_off_tk_thread_and_restores_stopped_state(self):
        a = self.app
        a._watch_state = {'active': True, 'source': str(self.root), 'output': str(self.root / 'output')}
        called = []
        with patch.object(a, '_watch_scan_once', side_effect=lambda state: (called.append(threading.get_ident()), ([], 'Watch done'))[-1]), patch.object(a, '_save_watch_state'):
            a._watch_tick()
            a._watch_state['active'] = False
            deadline = time.monotonic() + 3
            while a._watch_busy and time.monotonic() < deadline:
                a.update()
                time.sleep(.01)
        self.assertNotEqual(called, [threading.get_ident()])
        self.assertFalse(a._watch_state['active'])
        self.assertFalse(a._watch_busy)

    def test_iso_actions_and_incremental_selection(self):
        a = self.app
        iso = self.root / 'output.iso'
        iso.write_bytes(b'fixture')
        a._job_rows['one'] = (self.root / 'job.json', {'output': str(iso), 'source': str(self.root / 'source')})
        update_row(a.jobs_tree, 'one', ('completed', '100', 'Film', 'cq:20', 'copy', str(iso)))
        a.jobs_tree.selection_set('one')
        update_row(a.jobs_tree, 'one', ('completed', '100', 'Film', 'cq:22', 'copy', str(iso)))
        self.assertEqual(a.jobs_tree.selection(), ('one',))
        with patch.object(a, '_run_cli') as run:
            a._validate_selected()
            self.assertEqual(run.call_args.args[0][0], 'verify-iso')
        with patch.object(core, 'discover_tools', return_value={'vlc': 'vlc.exe'}), patch.object(core, 'require_tool', return_value='vlc.exe'), patch.object(core.subprocess, 'Popen') as popen:
            core.cmd_play(argparse.Namespace(target=str(iso), region=None, json=False))
            self.assertIn('bluray:///', popen.call_args.args[0][-1])
            self.assertTrue(popen.call_args.args[0][-1].endswith('output.iso'))

    def test_actions_and_details_fit_minimum_window(self):
        a = self.app
        a.geometry('980x700')
        a.deiconify()
        a.notebook.select(a.jobs_tab)
        a.update()
        button = a._buttons['_copy_job_details'][0]
        self.assertTrue(button.winfo_ismapped())
        self.assertGreater(a.job_detail.winfo_height(), 40)
        self.assertLess(button.winfo_rooty() - a.winfo_rooty() + button.winfo_height(), a.winfo_height())


if __name__ == '__main__':
    unittest.main()
