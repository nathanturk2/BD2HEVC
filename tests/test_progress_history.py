import tempfile
import unittest
from pathlib import Path
from bd2hevc_app.runtime_support import read_progress_log


class ProgressHistoryTests(unittest.TestCase):
    def test_earlier_completed_markers_survive_a_large_log_and_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.log"
            path.write_text("BD2HEVC_PROGRESS encode-done 00001.m2ts\n" + "tool output\n" * 300000)
            text = read_progress_log(path, "BD2HEVC_PROGRESS ")
            self.assertIn("encode-done 00001.m2ts", text)
            self.assertLess(len(text), 2 * 1024 * 1024 + 100)
            with path.open("a") as stream: stream.write("BD2HEVC_PROGRESS encode-start 00002.m2ts\n")
            text = read_progress_log(path, "BD2HEVC_PROGRESS ")
            self.assertIn("encode-done 00001.m2ts", text)
            self.assertIn("encode-start 00002.m2ts", text)
            path.write_text("BD2HEVC_PROGRESS encode-start 00003.m2ts\n")
            self.assertNotIn("00001.m2ts", read_progress_log(path, "BD2HEVC_PROGRESS "))

    def test_utf16_progress_survives_tail_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.log"
            path.write_text("BD2HEVC_PROGRESS validate-done 00001.m2ts\n" + "noise\n" * 300000, encoding="utf-16")
            self.assertIn("validate-done 00001.m2ts", read_progress_log(path,"BD2HEVC_PROGRESS "))
