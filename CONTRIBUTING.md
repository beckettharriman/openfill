# Contributing to OpenFill

OpenFill is a GIMP 3 plug-in written in plain Python. The plug-in folder is
`gimp-openfill/`: the entry point `gimp-openfill.py` and the package
`openfill/`. Everything except `openfill/gimp_io.py` and the entry point
is GIMP-free and covered by unit tests that run on any Python 3.10 or newer.

## Quick checks before every commit

From the repository root:

```sh
python -m unittest discover -t . -s tests -v
uv tool run ruff@0.14.0 check .
uv tool run ruff@0.14.0 format --check .
```

The unit tests use only the standard library; nothing has to be installed.
Ruff runs through [uv](https://docs.astral.sh/uv/) (`uv tool run` fetches a
pinned ruff into uv's cache, so no virtual environment or pip install is
needed). The configuration lives in `pyproject.toml`; each ignored rule has a
comment that says why. To fix a file you are changing:

```sh
uv tool run ruff@0.14.0 check --fix path/to/file.py
uv tool run ruff@0.14.0 format path/to/file.py
```

Format only the files you touch, so unrelated changes do not end up in your
diff. CI (`.github/workflows/ci.yml`) runs the same three commands on Python
3.10 and 3.12, and pull requests must keep them green.

`tests/check_catalog_urls.py` is a network check, not a unit test: it sends a
HEAD request to every page, license and file URL in `models/catalog.json`.
Run it after editing the catalog (set `HF_TOKEN` in the environment to check
gated files).

## Testing against a real GIMP

The `tests/gimp_batch_*.py` scripts run inside GIMP through the python-fu-eval
batch interpreter and drive the registered procedures end to end. The exact
command line is at the top of each script, for example:

```bat
"C:\Users\<you>\AppData\Local\Programs\GIMP 3\bin\gimp-3.2.exe" --new-instance --no-splash ^
    --console-messages --batch-interpreter python-fu-eval ^
    -b "exec(open(r'C:\path\to\gimp-openfill\tests\gimp_batch_fill.py').read())"
```

Always pass `--new-instance`, so a GIMP you have open for real work is never
reused or closed by a test run.

### Isolated profile (GIMP3_DIRECTORY)

Run tests against a throw-away GIMP profile instead of your own. GIMP 3 uses
the folder named by the `GIMP3_DIRECTORY` environment variable as its profile:

```powershell
$env:GIMP3_DIRECTORY = "C:\scratch\gimp-profile-test"
powershell -ExecutionPolicy Bypass -File install.ps1 -Link      # junction into the test profile
# copy your openfill\config.json into C:\scratch\gimp-profile-test\openfill\ if you need
# your ComfyUI folder and model choice (never commit it: it may hold your token)
& "C:\Users\<you>\AppData\Local\Programs\GIMP 3\bin\gimp-3.2.exe" --new-instance
```

On Linux and macOS: `GIMP3_DIRECTORY=/tmp/gimp-profile-test sh install.sh --link`
(on macOS the GIMP binaries are in `/Applications/GIMP.app/Contents/MacOS/`, for
example `gimp-console-3.2`),
then start GIMP with the same variable set. The first start of a fresh profile
is slow (font cache); allow about two minutes. `install.ps1 -Uninstall` and
`sh install.sh --uninstall` remove the plug-in again (add `-RemoveConfig` or
`--remove-config` to delete the settings folder too).

Only one generation should run on a machine at a time; a second ComfyUI on
the same GPU fails or swaps heavily.

### Robustness and UI screenshot harnesses

Two harnesses cover what the generation scripts do not. Both are driven by
environment variables, which must be set before GIMP starts (the plug-in
processes inherit GIMP's environment, not the batch script's):

- `tests/gimp_batch_robustness.py` runs one failure scenario per GIMP start
  (backend not installed, port in use, cancel at each phase, backend crash,
  corrupt download, two GIMPs, ...). `OPENFILL_ROB_CASE` names the case (a
  `rob-...` id), `OPENFILL_ROB_OUT` the case's evidence folder. The scenario itself
  (a broken `config.json`, a listener on the port, a killed process) is set up
  by whoever starts GIMP; the script builds a small image with a selection,
  runs Generative Fill non-interactively and writes `gimp-report.json`,
  `before.png`, `after.png` and `plugin-log.txt`. Run it with
  `gimp-console-3.2.exe` and keep GIMP's stderr: in console mode the messages
  the user would see (`Gimp.message`) go there.
- `tests/gimp_batch_ui_screenshots.py` opens every dialog interactively in a
  GUI GIMP and captures each window, without generating, checking the backend
  or downloading. `OPENFILL_TEST_UI_FILE` names a JSON hooks file that the script
  rewrites before each dialog (`autoclose`, `settings_tab`, `advanced`,
  `mode`, `fake_variations`, `progress_demo`, `type` / `press`, `dry_run`; see
  `_test_hooks` in `gimp-openfill.py`). `OPENFILL_TEST_OUT` is the screenshot
  folder (default `docs/screenshots/`), `OPENFILL_REPO` the checkout, and
  `OPENFILL_TEST_MENU=1` also captures the Filters > OpenFill menu.

The plug-in reads a few more test-only variables: `OPENFILL_TEST_CANCEL_FILE` and
`OPENFILL_TEST_CANCEL_AT` press Cancel at a given progress step (`runner.py`), and
`OPENFILL_TEST_AUTOCLOSE` / `OPENFILL_SETTINGS_TAB` are older forms of the UI hooks. None
of them is set in normal use, and they never change what a normal run does.

### Evidence

Results of GIMP runs go under `docs/evidence/<run-or-date>/<case-id>/` (for
example `docs/evidence/overnight-2026-10-09/rob-port-in-use-8188/`), with
short kebab-case case ids whose prefix says what is varied: `sel-` (the
selection), `img-` (image type or size), `mode-` (operation or model),
`prompt-`, `photo-` (real photographs), `rob-` (robustness) and `err-`
(error messages). A case folder
normally holds `batch-command.txt` (the exact command), `report.json`
(inputs, timings, pass or fail, error text), `before.png` / `after.png`
downscaled to at most 1024 px on the long side, and `plugin-log.txt` (the
relevant `openfill.log` lines), plus crops when the region is small. A run
folder may also hold a run-wide spec (`matrix-spec.json`) and a
`quality-ledger.md` with one visual score row per case. Keep large originals
out of the repository and the new evidence of one run small (a few tens of
MB at most).
Never put a token, a full `config.json` or personal paths you do not want
published into evidence files.

## Coding rules

- GIMP image work (`Gimp.Image`, layers, selections, undo groups) lives in
  `openfill/gimp_io.py` and the entry point `gimp-openfill.py`. The GTK
  modules (`dialogs.py`, `ui_picker.py`, `models_ui.py`, `progress_window.py`,
  `runner.py`) may import `Gtk`, `Gdk` and `GLib`; of GIMP they use only
  `GimpUi.init`, the transient parent and the progress window handle, and
  `runner.py` the status-bar progress. Everything else must import without
  GIMP or GTK so it can be unit-tested.
- GTK is called only from the main thread. Worker threads (the generation in
  `runner.py`, Check backend in the Settings dialog) hand every UI update to
  the main loop with `GLib.idle_add`. Every `GLib.timeout_add` source is
  removed when its window closes, and a timeout callback must not raise
  (GLib drops a source whose callback raises).
- No new pip dependencies. GIMP's bundled Python has only the standard
  library (plus PyGObject); the plug-in must run there as shipped.
- Keep the code valid for Python 3.10 (`target-version = "py310"`).
- Subprocesses are started with argument lists, never `shell=True`.
- Line endings are LF (`.gitattributes` enforces it); files are UTF-8 without
  a byte-order mark.
- Public functions and classes get type hints and a docstring.
- Keep the Apache-2.0 headers (`# SPDX-License-Identifier: Apache-2.0`)
- No emojis in code, docs or commit messages.

## Adding a model to the catalog

Models are listed in `models/catalog.json`; the Models tab, the supervisor and
`install.ps1 -Model` all read it. An entry needs:

| field | meaning |
| --- | --- |
| `id` | unique, no spaces or slashes |
| `name` | shown in the Models tab |
| `tier` | `fast`, `quality`, `edit` or `removal` |
| `capabilities` | subset of `fill`, `expand`, `txt2img`, `remove`, `edit`, `patch` |
| `arch` / `template` | model family and workflow template (`null` for companion files) |
| `page` | the model's official page (https) |
| `license` | `{"name": ..., "url": ...}` of the weights' license |
| `gated` | true when Hugging Face requires accepting a license and a token |
| `source_type` / `uploader` | `first-party`, or `community-quantization` plus who converted it |
| `min_vram_gb` | rough minimum GPU memory |
| `requires_nodes` | custom node packs: `repo` (https://github.com/owner/name), `folder`, `commit` (full 40-character SHA), `license` |
| `requires_models` | ids of companion entries |
| `files` | `role`, `url` (https), `dest` (`<models subfolder>/<file name>`), `size` in bytes, `sha256`, optional `gated` / `optional` |

Rules the loader enforces (see `catalog.parse_catalog`): https URLs only;
`dest` is a relative path of plain names (no `..`, no drive, no absolute
path); `sha256` is 64 lowercase hex characters; node repos are GitHub URLs
pinned to a full commit. The unit tests additionally require a `size` and a
`sha256` for every file, so every download is verified before it is used.
Take the sha256 from the Hugging Face file page (the LFS "SHA256" value) or,
for hosts that publish none, compute it from a download you made yourself and
say so in the commit message. Check the license allows redistribution of the
download link and local use, link the original author's page rather than a
mirror, and run `tests/check_catalog_urls.py` before sending the change.

## Commit messages

One summary line in the imperative mood, at most about 72 characters
("Add LaMa removal model", "Fix expand when the canvas is indexed"), a blank
line, then a body that explains what changed and why: root causes, a short
list of the touched areas, and where the evidence is. Wrap the body at about
72 columns. One logical change per commit.

## Pull requests

- Keep the unit tests, `ruff check` and `ruff format --check` green.
- Add or update unit tests for every behaviour you change or fix.
- For changes that affect what happens inside GIMP, include evidence from a
  batch run (see above) or describe how you tested it by hand.
- Update `CHANGELOG.md` and, when user-visible, `README.md`.
- Security problems: see `SECURITY.md` instead of opening a public issue.
