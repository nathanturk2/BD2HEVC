"""Output folder, preservation-copy, and user-facing summary helpers."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .bitrate import format_duration
from .tools import ToolError


def make_output_available(output: Path, source: Path, *, force: bool) -> None:
    output = output.resolve()
    source = source.resolve()
    if output == source or source in output.parents:
        raise ToolError("Refusing to write output inside the source backup")
    anchor = Path(output.anchor)
    if output.anchor and not anchor.exists():
        raise ToolError(f"Output drive or root does not exist: {output.anchor}")
    if output.exists():
        if not force:
            raise ToolError(f"Output already exists: {output}. Use --force to replace it.")
        generated_disc_name = "(uhd converted)" in output.name.lower()
        if len(output.parts) < 4 and not (len(output.parts) >= 3 and generated_disc_name):
            raise ToolError(f"Refusing to remove suspicious output path: {output}")
        if output.is_dir():
            shutil.rmtree(output)
        else:
            output.unlink()


def is_bdmv_stream_file(relative_path: Path, filenames: set[str]) -> bool:
    parts_upper = [part.upper() for part in relative_path.parts]
    return (
        len(relative_path.parts) == 3
        and parts_upper[0] == "BDMV"
        and parts_upper[1] == "STREAM"
        and relative_path.name in filenames
    )


def copy_disc_tree_skipping_reencoded_streams(source: Path, output: Path, reencode_files: set[str]) -> dict[str, Any]:
    copied_files = 0
    skipped_files = 0
    copied_bytes = 0
    skipped_bytes = 0
    output.mkdir(parents=True, exist_ok=True)
    for src_dir, dirnames, filenames in os.walk(source):
        src_dir_path = Path(src_dir)
        relative_dir = src_dir_path.relative_to(source)
        dst_dir_path = output / relative_dir
        dst_dir_path.mkdir(parents=True, exist_ok=True)
        for dirname in dirnames:
            (dst_dir_path / dirname).mkdir(exist_ok=True)
        for filename in filenames:
            src_file = src_dir_path / filename
            relative_file = src_file.relative_to(source)
            size = src_file.stat().st_size
            if is_bdmv_stream_file(relative_file, reencode_files):
                skipped_files += 1
                skipped_bytes += size
                continue
            dst_file = output / relative_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            copied_files += 1
            copied_bytes += size
    return {
        "copied_files": copied_files,
        "copied_bytes": copied_bytes,
        "skipped_reencode_stream_files": skipped_files,
        "skipped_reencode_stream_bytes": skipped_bytes,
    }


def replace_file_with_retry(source: Path, target: Path, *, attempts: int = 20, delay_seconds: float = 2.0, verbose: bool = False) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            for path in (source, target):
                if path.exists():
                    path.chmod(path.stat().st_mode | 0o200)
            os.replace(source, target)
            return {"source": str(source), "target": str(target), "attempts": attempt, "replaced": True}
        except PermissionError as exc:
            last_error = exc
            if verbose:
                print(f"replace retry {attempt}/{attempts} for {target}: {exc}", file=sys.stderr, flush=True)
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise ToolError(
        f"Could not replace {target} after {attempts} attempts. "
        "Close any player or scanner using the output backup and rerun repair-output."
    ) from last_error


def default_output_for(source: Path, mode: str) -> Path:
    suffix = "FULL_DISC_HEVC" if mode == "clone-streams" else "UHDBD_MOVIE_ONLY_HEVC"
    return source.resolve().parent / f"{source.name}_{suffix}"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "disc"


def path_or_none(value: str | None) -> Path | None:
    return Path(value).resolve() if value else None


def conversion_succeeded(result: dict[str, Any], *, require_makemkv: bool = False, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    mode = result.get("mode")
    if mode == "movie-only":
        if not (result.get("validation") or {}).get("ok"):
            return False
        makemkv = result.get("makemkv_validation")
        return not (require_makemkv and makemkv and not makemkv.get("ok"))
    if mode == "clone-streams":
        if not all(v.get("ok") for v in result.get("validation", [])):
            return False
        makemkv = result.get("makemkv_validation") or {}
        return not (require_makemkv and not makemkv.get("ok"))
    return True


def format_path(path: str | Path | None) -> str:
    return str(path) if path else "(none)"


def print_conversion_summary(
    result: dict[str, Any],
    *,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    output = result.get("output")
    source = result.get("source")
    mode = result.get("mode")
    if dry_run:
        clips = result.get("reencode_clips") or []
        total = sum(float(clip.get("duration") or 0) for clip in clips)
        print("BD2HEVC conversion plan")
        print(f"Source: {format_path(source)}")
        print(f"Output: {format_path(output)}")
        print(f"Mode: full disc, menus and extras preserved" if mode == "clone-streams" else f"Mode: {mode}")
        print(f"Clips to reencode: {len(clips)} ({format_duration(total)} of video)")
        if clips:
            preview = ", ".join(str(clip.get("file")) for clip in clips[:8])
            if len(clips) > 8:
                preview += f", ... +{len(clips) - 8} more"
            print(f"First clips: {preview}")
        disc_fit = result.get("target_disc_fit") or {}
        if disc_fit:
            fit_text = "scaled to fit" if disc_fit.get("scaled") else "already fits"
            target = disc_fit.get("target_size")
            estimated = disc_fit.get("estimated_after_bytes") or disc_fit.get("estimated_before_bytes")
            if estimated:
                print(f"Target disc size: {target} ({fit_text}, estimated {estimated / 1_000_000_000:.2f} GB)")
            else:
                print(f"Target disc size: {target} ({fit_text})")
        if report_path:
            print(f"Plan saved to: {report_path}")
        print("Run again without --dry-run to start the conversion.")
        return

    print("BD2HEVC conversion complete")
    print(f"Output: {format_path(output)}")
    if mode == "clone-streams":
        reencoded = result.get("reencoded") or []
        validation = result.get("validation") or []
        failed = [Path(v.get("output", "")).name for v in validation if not v.get("ok")]
        print(f"Reencoded clips: {len(reencoded)}")
        print("Clip validation: " + ("passed" if not failed else f"failed ({', '.join(failed[:8])})"))
        disc_fit = result.get("target_disc_fit") or {}
        if disc_fit:
            fit_text = "scaled to fit" if disc_fit.get("scaled") else "already fit"
            print(f"Target disc size: {disc_fit.get('target_size')} ({fit_text})")
        uhd_structure = result.get("uhd_structure") or {}
        if uhd_structure:
            created = len(uhd_structure.get("created_dirs") or [])
            version_patches = sum(1 for item in uhd_structure.get("version_patches") or [] if item.get("patched"))
            header_target = uhd_structure.get("version_header_target")
            header_action = "UHD headers patched" if header_target == "uhd" else "BD-style headers restored"
            print(f"UHD profile: applied ({created} folders created, {version_patches} {header_action})")
        makemkv = result.get("makemkv_validation") or {}
        if makemkv.get("skipped"):
            print("MakeMKV validation: skipped")
        elif makemkv:
            print("MakeMKV validation: " + ("passed" if makemkv.get("ok") else "failed"))
    elif mode == "movie-only":
        title = result.get("selected_title") or {}
        print(f"Selected title: {title.get('id', '(unknown)')}  duration: {format_duration(title.get('duration'))}")
        print("Validation: " + ("passed" if (result.get("validation") or {}).get("ok") else "failed"))
    if report_path:
        print(f"Full report saved to: {report_path}")


def xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


TITLE_LOWERCASE_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "into",
    "nor",
    "of",
    "on",
    "or",
    "per",
    "the",
    "to",
    "via",
    "vs",
    "with",
}

FALLBACK_TITLE_ACRONYMS = {
    "AC3",
    "BBC",
    "BD",
    "BDMV",
    "DTS",
    "DVD",
    "HD",
    "HEVC",
    "OVA",
    "UHD",
    "UK",
    "USA",
}

ROMAN_NUMERAL_RE = re.compile(r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", re.IGNORECASE)


def load_title_acronyms() -> set[str]:
    path = Path(__file__).with_name("data") / "title_acronyms.txt"
    acronyms = set(FALLBACK_TITLE_ACRONYMS)
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            token = line.strip()
            if not token or token.startswith("#"):
                continue
            if re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", token):
                acronyms.add(token)
    except OSError:
        pass
    return acronyms


TITLE_ACRONYMS = load_title_acronyms()


def is_roman_numeral(value: str) -> bool:
    return bool(value) and bool(ROMAN_NUMERAL_RE.fullmatch(value)) and any(ch in value.upper() for ch in "IVXLCDM")


def should_use_acronym_case(core: str, title_has_lowercase: bool) -> bool:
    upper = core.upper()
    if upper not in TITLE_ACRONYMS:
        return False
    if core == upper or not title_has_lowercase:
        return True
    return len(core) >= 3 and core.islower()


def smart_title_word(word: str, index: int, total: int, *, title_has_lowercase: bool) -> str:
    match = re.fullmatch(r"([^A-Za-z0-9]*)([A-Za-z0-9]+(?:'[A-Za-z0-9]+)?)([^A-Za-z0-9]*)", word)
    if not match:
        return word
    prefix, core, suffix = match.groups()
    upper = core.upper()
    lower = core.lower()
    if is_roman_numeral(upper) or should_use_acronym_case(core, title_has_lowercase):
        normalized = upper
    elif core == upper and not title_has_lowercase and len(core) <= 3 and lower not in TITLE_LOWERCASE_WORDS:
        normalized = upper
    elif re.fullmatch(r"[A-Z]+\d+[A-Z0-9]*|\d+[A-Z]+[A-Z0-9]*", upper):
        normalized = upper
    elif 0 < index < total - 1 and lower in TITLE_LOWERCASE_WORDS:
        normalized = lower
    else:
        normalized = core[:1].upper() + core[1:].lower()
    return f"{prefix}{normalized}{suffix}"


def smart_title_case(title: str) -> str:
    words = title.split()
    title_has_lowercase = any(ch.islower() for ch in title)
    return " ".join(smart_title_word(word, index, len(words), title_has_lowercase=title_has_lowercase) for index, word in enumerate(words))


def disc_title_from_folder_name(name: str) -> str:
    title = name
    title = re.sub(r"\s*\(BD\)\s*\(UHD converted\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(UHD converted\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(BD\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"_(FULL_DISC_HEVC|UHDBD.*)$", "", title, flags=re.IGNORECASE)
    title = title.replace("_", " ").strip(" .-_")
    title = re.sub(r"\s+", " ", title).strip()
    title = smart_title_case(title) if title else title
    return title or "Blu-ray Disc"


def existing_disc_metadata_files(root: Path) -> list[Path]:
    meta_dir = root / "BDMV" / "META" / "DL"
    if not meta_dir.is_dir():
        return []
    return sorted(path for path in meta_dir.glob("bdmt_*.xml") if path.is_file() and path.stat().st_size > 0)


def ensure_disc_library_metadata(root: Path, *, title: str | None = None, force: bool = False) -> dict[str, Any]:
    root = root.resolve()
    existing = existing_disc_metadata_files(root)
    if existing and not force:
        return {
            "root": str(root),
            "created": False,
            "updated": False,
            "title": None,
            "files": [str(path) for path in existing],
            "reason": "existing metadata present",
        }
    meta_dir = root / "BDMV" / "META" / "DL"
    meta_dir.mkdir(parents=True, exist_ok=True)
    display_title = title or disc_title_from_folder_name(root.name)
    escaped_title = xml_escape(display_title)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<disclib xmlns="urn:BDA:bdmv;disclib">
  <di:discinfo xmlns:di="urn:BDA:bdmv;discinfo">
    <di:title>
      <di:name>{escaped_title}</di:name>
      <di:numSets>1</di:numSets>
      <di:setNumber>1</di:setNumber>
    </di:title>
    <di:description>
      <di:tableOfContents>
        <di:titleName titleNumber="1">{escaped_title}</di:titleName>
      </di:tableOfContents>
    </di:description>
    <di:language>eng</di:language>
  </di:discinfo>
</disclib>
"""
    target = meta_dir / "bdmt_eng.xml"
    target.write_text(xml, encoding="utf-8", newline="\n")
    return {
        "root": str(root),
        "created": not existing,
        "updated": bool(existing),
        "title": display_title,
        "files": [str(target)],
        "reason": "generated fallback metadata" if not existing else "metadata overwritten",
    }
