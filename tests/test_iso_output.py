from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bd2hevc_app.core import build_parser
from bd2hevc_app.gui import generated_output
from bd2hevc_app.iso import (
    folder_stats,
    iso_output_path,
    iso_partial_path,
    iso_progress_path,
    iso_staging_path,
    validate_bluray_folder,
    volume_label,
)
from bd2hevc_app.queueing import auto_command_for_job


class IsoOutputTests(unittest.TestCase):
    def test_generated_iso_name_preserves_source_name_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Star Trek TNG - Season 1, Disc 1"
            source.mkdir()
            output = generated_output(source, root / "out", output_format="iso")
            self.assertEqual(output.name, "Star Trek TNG - Season 1, Disc 1 (BD) (UHD converted).iso")

    def test_start_queue_propagates_iso_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["start", "source", "output.iso", "--output-format", "iso"])
        command = auto_command_for_job(args, Path("output.iso"), Path("report.json"), Path("plan.json"))
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "iso")

    def test_bdmv_validation_and_staging_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Disc"
            for relative in ("BDMV/PLAYLIST", "BDMV/STREAM"):
                (source / relative).mkdir(parents=True, exist_ok=True)
            (source / "BDMV/index.bdmv").write_bytes(b"index")
            (source / "BDMV/MovieObject.bdmv").write_bytes(b"movie")
            (source / "BDMV/PLAYLIST/00000.mpls").write_bytes(b"playlist")
            (source / "BDMV/STREAM/00000.m2ts").write_bytes(b"stream")
            validate_bluray_folder(source)
            files, directories, size = folder_stats(source)
            self.assertEqual(files, 4)
            self.assertEqual(directories, 3)
            self.assertGreater(size, 0)
            output = iso_output_path(Path(temporary) / "Movie")
            self.assertEqual(output.suffix, ".iso")
            self.assertEqual(iso_staging_path(output).name, ".Movie.iso.bd2hevc-folder.part")
            partial = iso_partial_path(output)
            self.assertEqual(iso_progress_path(partial).name, ".Movie.iso.bd2hevc-iso.part.progress")

    def test_volume_label_is_bounded_and_portable(self) -> None:
        label = volume_label("Pride & Prejudice: Collector's Edition 2005")
        self.assertLessEqual(len(label), 32)
        self.assertRegex(label, r"^[A-Z0-9_-]+$")


if __name__ == "__main__":
    unittest.main()
