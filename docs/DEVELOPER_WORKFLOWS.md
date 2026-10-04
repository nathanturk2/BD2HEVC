# Developer workflows

Start with the current README for normal conversion. Public workflows use `gui`, `tools`, `auto`, `start`, `queue`, `jobs`, `status` and the documented playback/verification commands. Older phase/prototype commands remain available for engineering investigations; they are not the first-run path.

## Structure and ownership

The public CLI facades remain compatible with existing scripts and tests. Focused workflow modules receive an explicit `services` context, so their filesystem/process boundaries remain injectable without duplicating converter logic.

- `cli_parser.py`: command definitions and help.
- `planning_workflow.py`: source/clip planning.
- `conversion_workflow.py`: full-disc conversion orchestration.
- `clip_listing.py`: planning reports and clip listing.
- `core.py`: compatibility facade and remaining command orchestration.
- `queueing.py` / `locking.py`: coordinated job records and kernel work-slot locks.
- `repair_transaction.py`: paired clip/CLPI rollback.
- `iso_integrity.py`: file-level authored-image payload verification.

Legacy `convert --mode movie-only` creates a newly authored title rather than preserving the complete disc. `remux-replacements`, `reencode-replacements`, `repair-*`, `patch-vlc-compat`, `record-libbluray` and UDF repair commands are specialist maintenance tools; inspect their help and keep backups before an in-place repair.

## Shared modules

`gui_support.py`, `udf_validation.py` and `runtime_support.py` are deliberately shipped independently in both projects. Change both copies together. `SHARED_MODULES.json` records normalized AST hashes; the GUI's platform import adapter is the only excluded difference. Run `python tools/check-shared.py PATH_TO_SIBLING_REPO` before updating the manifests. No shared package installation is required.

Raw progress output is bounded. An incremental reader scans new bytes and retains compact task events across the raw tail boundary, including UTF-16 PowerShell logs. Rotation/truncation resets its cache. Subprocess output and diagnostic tails must remain bounded, and cancellation/error paths must reap their children.

## Local checks

```powershell
python -m unittest discover -s tests
python tools/check-shared.py
python tools/check-provenance.py
python tools/build-release.py --require-clean
```

The release workflow tests exported artifacts, not just the checkout. See the release guide for installed-resource checks and the private playback-matrix template.

`tools/make-demo.py` creates a 16-second FFmpeg-generated disc and can run the real converter with `--convert`. It refuses existing destinations. `tools/demo-ui.py` is solely a labelled screenshot harness with isolated state and simulated jobs. It must not be used as evidence of a real conversion.
