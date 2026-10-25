# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Remove with LaMa failed the first time on a ComfyUI that did not have
  OpenFill's node pack yet (one you set up yourself, say), with "needs the
  custom node pack(s) openfill-nodes; press Install nodes in the Models tab",
  until another job had started ComfyUI. OpenFill now puts its own node pack
  in place before ComfyUI starts.

## [2.0.0-beta.4] - 2026-10-25

### Added

- Two erase-trained models in the Models tab, to try against LaMa. Both are
  in the Remove model menu once downloaded. I have not compared them on real
  photos yet.
  - RORem (Apache-2.0, 6.9 GB): an SDXL inpainting model trained to remove
    objects (CVPR 2025). It runs on ComfyUI's own nodes, needs about 8 GB of
    graphics memory, and works a large selection in tiles at full resolution.
  - FLUX.1 Fill object removal LoRA v1 (0.3 GB, on top of FLUX.1 Fill): the
    LoRA is MIT, but FLUX.1 Fill dev is non-commercial, so this is for
    non-commercial use only. It runs on whichever FLUX.1 Fill you have (the Q4
    or Q6 GGUF, the full model, or your own file); with none, Download adds the
    Q4 GGUF. Slow on 8 GB, like FLUX Fill itself.

### Changed

- Remove object works at the image's own resolution. The node that ran LaMa
  squeezed every area into a 256 px square, so on a large photo the result
  came back soft: a 3150 x 1800 px selection on a 4344 x 3258 px image was
  cleared at 512 x 384 px and stretched back up. OpenFill now comes with its
  own small ComfyUI node that runs LaMa on the selection and its surroundings
  at 1:1, padded to a multiple of 8, in one pass when it fits the graphics
  memory. When it doesn't, it works in a few large overlapping windows at 1:1
  and blends them; only a hole too wide for that is done once at the largest
  size that fits (about two thirds of full size for that whole-car selection
  on an 8 GB card, where it used to be about an eighth). On an RTX 4070 Laptop
  GPU a 1300 x 1360 px area took one full-size pass in about 5 s and 1.6 GB of
  graphics memory. The node goes into ComfyUI's custom_nodes folder as
  openfill-nodes whenever OpenFill starts ComfyUI, and the Models tab offers
  Install nodes for LaMa if it is missing. Half precision is not used: LaMa
  returns a flat patch in fp16. MAT is fixed at 512 px and works as before.
- Any model can remove. The model you pick for Remove object is used as
  picked: LaMa stays the default, and MAT, the erasers and every fill model
  (FLUX and Qwen too) are offered. A fill model gets a background-only prompt.
  Fill lists the removal models as well; with an empty prompt they remove.
- The Models list says what each model can run, for example "Fill, expand,
  create, remove" or "Remove (erase-trained)", next to its size.
- PhotoCraft: Remove no longer runs LaMa and then a generation model over its
  result. With LaMa at full resolution that second pass isn't needed, and it
  took minutes. The Remove menu keeps the model choice: LaMa (default), MAT,
  the erasers and each generation model, used as picked. Picking a removal
  model in Fill uses it for that run without making it the active model.
- License: Apache-2.0 (was GPL-3.0-or-later). OpenFill started as a GIMP 3
  port of Torben Giesselmann's GIMP plug-in; none of its code remains, and the
  README keeps the credit.
- The repository is now beckettharriman/openfill (was gimp-openfill) and also
  holds OpenFill for PhotoCraft, which shares this engine.
- A removal's layer is called Object Removal, whichever model made it. With an
  eraser it was named after the eraser's own trigger words ("Generative Fill:
  4K, high quality, masterpiece..."), and a fill model used to remove was
  named after OpenFill's background prompt.
- ComfyUI is also looked for in `~/AI/ComfyUI`.

### Fixed

- A busy ComfyUI that did not answer one status request for a moment (seen on
  Windows right after a fill finished) failed the job with "The AI backend is
  not available". While waiting for a result, a server that gives no answer
  at all now counts as "not done yet" for up to 90 seconds.

## [2.0.0-beta.3] - 2026-10-21

### Added

- Your own models: SD 1.5 inpainting and SDXL checkpoints in ComfyUI's
  `checkpoints` folder and FLUX.1 Fill files in `unet` or `diffusion_models`
  that the catalog doesn't list are found and offered as "your file", in the
  Fill, Expand and Text to Image model menus and in the Models tab, with no
  download. Only `.safetensors` and `.gguf` headers are read (pickled files
  are never opened); the family comes from the shapes of a few tensors (SD 1.5
  inpainting: a 9-channel input at 320 features with 768-wide text attention;
  SDXL: the label embedding; FLUX.1 Fill: a 384-wide image input). Each file
  takes the settings and companion files of the bundled model of its family
  (the Fooocus patch, FLUX's text encoders and VAE). Files that don't fit
  (regular SD 1.5, SDXL inpainting, FLUX.1 dev, SD 2) are skipped and logged
  with the reason.
- Models chosen from the live Hugging Face and Civitai search can be picked
  in the dialogs' model menu too, not only made active in the Models tab.

## [2.0.0-beta.2] - 2026-10-19

From the first user reports: a ComfyUI running on the processor without a
word, FLUX fills that looked stuck, FLUX prompts that painted the wrong
things, and GTX 10 cards that failed every job.

### Added

- OpenFill logs the device ComfyUI runs on at every start and, when it is
  the processor, says so once and why: `--cpu` in Extra ComfyUI options, or
  a PyTorch that cannot use the graphics card. Settings > Check backend ends
  with "Running on: ..." and flags the processor.
- Settings > Open log folder: openfill.log and comfyui.log, the console of
  the hidden ComfyUI.
- Live preview files: SD 1.5, SDXL and FLUX models download their small
  TAESD preview decoder (about 5 MB, MIT) with them, so the progress window
  shows the image forming. A model downloaded earlier fetches its decoder in
  the background before its next job.
- FLUX.1 Fill GGUF Q6_K for 12 GB cards, sharper than the Q4_0 for 8 GB.
- The Fill dialog reminds FLUX users to describe only what goes in the
  selection: FLUX paints everything the prompt names.

### Fixed

- Older NVIDIA cards (GTX 10 series and earlier) work: every job used to
  fail at once with "The GPU ran out of memory" (issue #2). The ComfyUI
  that Download ComfyUI installs ships a PyTorch made for RTX 20 cards and
  newer; OpenFill now reads the card's compute capability and fetches the
  same ComfyUI release built for older cards (CUDA 12.6) instead.
- A CUDA error is no longer reported as running out of memory. Real
  out-of-memory errors keep ComfyUI's own first line, and a ComfyUI whose
  PyTorch cannot run the card says so, once after it starts and on any job,
  with what to download.
- The `--cpu` that Download ComfyUI adds on a PC without an NVIDIA card is
  removed once OpenFill uses another ComfyUI folder, instead of keeping that
  one on the processor.

## [2.0.0-beta.1] - 2026-10-17

First public beta, and the first release under the name OpenFill: model
management from inside GIMP, a redesigned prompt-first UX with progress,
preview and cancel, a hardened backend and downloader, and fixes from
end-to-end runs in a real GIMP 3.2.6 on Windows, Linux (Flatpak) and macOS
(`docs/evidence/`).

### Added

- Model catalog (`models/catalog.json`): SD 1.5 inpainting, SDXL base,
  Fooocus inpaint patch, Juggernaut XL v9, FLUX.1 Fill [dev] (GGUF Q4_0
  community quantization and gated first-party safetensors), Qwen-Image-Edit-2511
  GGUF Q4_K_M, LaMa and MAT for prompt-free removal, each with license,
  source type, gating flag, sizes, sha256 and pinned custom nodes.
- Models tab in the Settings dialog: one row per model, downloaded models
  first, one primary button that follows the state (Download, Downloading N% -
  Cancel, Resume download, Set active, Active, Install nodes), a menu for the
  model page, the license and deleting files, Open models folder, and a
  Hugging Face token field shown only when a gated model needs it (stored in
  the profile `config.json` only).
- Windowless downloader in the supervisor: queue, HTTP Range resume, sha256
  verification, atomic rename, disk-space and gated (401/403) errors, cancel;
  a tasks-only supervisor mode when the backend is not running.
- On-demand custom node installation (the GitHub zip of the pinned commit, no
  git needed, checked against the pin; pip install with ComfyUI's Python) with confirmation and backend restart;
  ComfyUI-GGUF for the FLUX and Qwen tiers.
- Installing on a PC with nothing set up: `Install OpenFill.cmd` (double-click)
  runs `install.ps1`, which creates the GIMP profile folder if GIMP has not been
  started yet and looks for an existing ComfyUI (portable folders, ComfyUI
  Desktop, Stability Matrix, Pinokio; `install.ps1 -DetectComfy`). When
  ComfyUI is missing, the first run and every Generative Fill, Remove or
  Expand shows a short "ComfyUI is required" notice with **Download ComfyUI**
  (size shown) and "I already have ComfyUI". Before the download it checks the
  graphics card, free disk space and port 8188 and says plainly when there is
  no NVIDIA card (processor-only is offered as very slow; AMD gets a link to
  ComfyUI's AMD instructions). The download is a pinned, checksummed official
  portable release (`models/comfy_setup.json`), unpacked with Windows' own
  `tar.exe`, plus the inpaint nodes at their pinned commit and a pinned OpenCV,
  run as a supervisor task with progress in the progress window.
- `tools/make_release_zip.py` builds the release zip (plug-in, catalog,
  setup list, installer scripts, README, COPYING; no tests or evidence).

- Workflow templates selected by the active model: FLUX Fill (GGUF or fp8
  loaders), Qwen-Image-Edit, LaMa/MAT removal (prompt skipped), SDXL
  checkpoint swap for Juggernaut.
- `install.ps1 -ListModels` and `-Model <id> [-HfToken]` for headless parity;
  `-ProfileDir` / `--profile-dir`, `-RemoveConfig` / `--remove-config` and
  `GIMP3_DIRECTORY` support; `install.sh --uninstall` and Linux, Flatpak and
  macOS profile detection.
- Remove object mode in Generative Fill (routes to LaMa/MAT when installed,
  otherwise a background prompt with border pre-fill).
- Floating progress window (`progress_window.py`, `runner.py`): always on top
  without taking the focus, placed over the image window, position remembered
  and moved back on screen when its monitor is gone; plain step text, percent,
  elapsed and remaining time, live TAESD preview from ComfyUI's websocket
  (throttled), download rows, and Cancel (interrupt and clear the queue, image
  untouched). Generation runs in a worker thread.
- Redesigned dialogs (`dialogs.py`): prompt-first Generative Fill, Expand and
  Text to Image with a large multi-line prompt, a Fill with a prompt / Remove
  object switch, Recent prompts (last 20, persisted), an Advanced section
  (Model, Avoid, Variations, Change amount, Seed), a status line, Enter to
  generate, Shift+Enter for a new line, Escape to cancel. Non-interactive PDB
  calls keep the same arguments.
- Variations picker after Fill or Expand with several variations: thumbnails,
  1 to 9 to pick, Keep selected, Keep all, Discard (one undo step).
- Settings General tab with AI backend folder and Browse, Start backend
  automatically, Idle timeout, Show the floating progress window and a
  threaded Check backend button with a readable result; the remaining options
  behind Advanced generation and Connection and troubleshooting expanders.
- Debug dump of the exact crop, mask and raw result (`debug` setting or
  `SB_DEBUG_DUMP=1`).
- `pyproject.toml` (ruff configuration), GitHub Actions CI (ruff and unit
  tests on Python 3.10 and 3.12), `CONTRIBUTING.md`, `SECURITY.md`,
  `docs/security.md`, `docs/performance.md`.
- Tests: catalog validation, resumable downloads, template selection, preview
  parsing, ETA and frame throttling, dialog text logic, config recovery,
  port probing, supervisor argument parsing, geometry helpers; GIMP batch
  harnesses for the case matrix, robustness, the progress window and the UI
  screenshots.

### Changed

- CI also runs the unit tests on Windows (next to Linux and macOS).
- Standard GIMP install is now the headline: the release zip
  (`openfill-<version>.zip`) holds a self-contained `gimp-openfill/` folder
  (code, `models/catalog.json`, `models/comfy_setup.json`, README, COPYING)
  that is copied into GIMP's plug-ins folder, with no script. Verified on an
  empty profile with gimp-console: all procedures register. Settings are
  created on first change; defaults apply until then. `Install OpenFill.cmd`
  and `install.ps1` stay as the optional path (uninstall, link, model
  downloads).
- Generative Expand crops and expands in one frame: dragging an edge inward
  past the photo crops that side, ratio buttons (Original, 1:1, 5:4, 4:3, 3:2,
  16:9, Custom) with a Landscape/Portrait switch keep the frame's ratio while
  it is moved or resized, and the anchor grid snaps it. Several expand rounds
  share one progress window and one overall progress bar.
- Renamed the project to OpenFill: menu Filters > OpenFill, plug-in
  folder and entry script `gimp-openfill/gimp-openfill.py`, package
  `openfill`, procedures `python-fu-openfill-*`, settings folder
  `<profile>/openfill` with `openfill.log`, test variables `OPENFILL_*`.
  Settings in `<profile>/stable-boy` are copied to `<profile>/openfill` on
  the first run.
- OpenFill is based on Stable Boy by Torben Giesselmann (GPL-3.0),
  https://github.com/thndrbrrr/gimp-stable-boy.
- Generative Expand renders all sides in one pass, splits large expansions
  into rounds, and uses the model you pick. When the Expand default (SD 1.5
  inpainting, then DreamShaper 8) is installed and you picked another model,
  a short question offers to switch. The editor warns above 25 % growth per
  side.
- Default models per operation, from a judged benchmark of 33 cases per model:
  Fast fill DreamShaper 8, Quality fill RealVisXL V5, Remove LaMa, Expand SD
  1.5 inpainting. FLUX Fill stays opt-in and is never used for removal.
- Fills below working scale or over large selections are tiled (a layout pass
  first, then tiles); LaMa and MAT removal of large holes runs over
  overlapping bands. Border pre-fill, grain matching and variation ranking
  improve seams and sharpness.
- The first-run download manager recommends LaMa and DreamShaper 8 and names
  RealVisXL V5 as the upgrade.
- Shorter shortcut hint in the Fill dialog.
- The dialogs' Model field offers the active model, then downloaded catalog
  models, then the rest; the legacy fast/quality tiers remain for scripts.
- Every error message ends with a plain next step (`ui_text.with_next_step`,
  `backend.describe_error`); unexpected errors read "Something went wrong:
  ..." instead of a bare exception name. Settings labels match the messages
  (Start backend automatically, Check backend).
- Prompted fills use a tighter context (a margin of 0.5x the selection instead
  of 1x). The pre-fill depends on the tier: SD 1.5 gets a neutral grey hole,
  SDXL-tier models get the border pre-fill (a grey hole made Juggernaut paint
  a framed picture into the selection). Removal with a generation model keeps
  the wide context and the border pre-fill.
- The removal models (LaMa, MAT) work at a 512 px working size with a 0.5x
  margin, the size their node uses internally; the old 1024 px crop was
  shrunk inside the node and came back soft, with ghosts.
- A one-sided Generative Expand sends a local band of the old image next to
  the new strip as context instead of the whole canvas.
- One backend supervisor per GIMP profile (a start lock), and ComfyUI stays up
  while any GIMP that used it is still running.
- Cancel ends the command quietly (no error dialog, no traceback in the log);
  a message appears only when the job was stopped from outside GIMP.
- Previewing variations in the picker no longer adds undo steps: the fill is
  one undo step, and Discard is one more.
- Messages about a missing model use the short catalog name (for example
  "FLUX.1 Fill [dev]") instead of the packaging details.
- Messages no longer repeat the next step twice; the timeout advice names the
  real field, "Generation time limit".
- The Models tab shows failures of Cancel, Install nodes and Delete
  downloaded files in the tab, and keeps polling after a failed refresh.
- The layer mask of a prompted fill grows only by the feather radius, so it is
  opaque over the selection and fades outward; removal keeps the full grow.
- ComfyUI is started with `--preview-method taesd`; extra server arguments are
  passed as one argument so values such as `--cpu` work.
- Dialogs open centred on the image window.
- Crop PNGs are encoded at zlib level 1 (about half the encode time) and
  uploads are named by content, so re-rolling the same region with a new
  seed or prompt skips the image and mask preparation. Measured timings are
  in `docs/performance.md` (cold start 31 to 38 s, warm SD 1.5 fills 8 to
  11 s, SDXL tiers 20 to 28 s, MAT 3 to 6 s, no memory growth over 20 fills).

### Fixed

- Fills and Generative Expand no longer leave a faint outline on smooth
  areas. The generated layer drifted a few levels off the original at its
  edge (on a 4000 x 3000 sky: a 5-level step around the fill, about 8 levels
  at an expand's old edge beside a photo's sky); OpenFill now measures that
  drift where the layer must match the original and evens it out across the
  layer, leaving the generated detail alone (`seam_correction`, on by
  default; evidence in `docs/evidence/seam-2026-10-16/`).
- The graphics card is read from NVIDIA's management library (NVML) when
  `nvidia-smi` is missing, as it is inside the GIMP Flatpak, so the Models
  tab sizes models on the first run there too.
- A fill or Generative Expand made without OpenCV on the server (the result
  starts from a flat colour) now says so once, and OpenFill installs the
  pinned `opencv-python-headless` into ComfyUI's Python in the background.
  Installing the inpaint nodes also installs it when it is missing.
- A ComfyUI venv without pip (made with `uv venv`) gets pip from
  `ensurepip` before OpenFill installs node requirements or OpenCV into it.
- ComfyUI is also found in Stability Matrix's Linux library
  (`~/.local/share/StabilityMatrix`) and through ComfyUI Desktop's settings
  on macOS and Linux. `install.sh` notes a ComfyUI without OpenCV.
- macOS: the supervisor and the task worker start. GIMP 3.2 signs the
  Python in GIMP.app with a launch constraint, so the kernel killed them
  when the plug-in started them with its own interpreter; the backend never
  started and the Models tab never downloaded. They now run on ComfyUI's
  Python (or a python3 on PATH outside the app bundle), without GIMP's
  PYTHONHOME.
- macOS: GIMP processes are recognised (libproc), so the backend stops when
  GIMP closes even if its pid is reused.
- macOS and Linux: the "ComfyUI is required" notice no longer offers the
  Windows-only download; it links ComfyUI's install steps for the platform.
  A checkout in `~/ComfyUI` and Stability Matrix on macOS are found.
- Model downloads no longer get stuck when the task worker dies or stops
  mid-download (killed, or shut down with GIMP): an exited worker that is
  still a zombie of the Models tab counts as gone, a new worker re-queues
  the half-done task and resumes it from its `.part` file, and the Models
  tab no longer shows a download that nobody is doing.
- macOS: the Models tab sizes models on the first run: an Apple silicon
  Mac reports its chip and unified memory before ComfyUI has started.
- A ComfyUI folder that cannot be started says why ("main.py is there but
  no venv", "the folder does not exist", ...) instead of timing out; on
  macOS GIMP's own Python is never used to run ComfyUI.
- "Nothing is selected" and the indexed-image message no longer end with
  "try again ... keep openfill.log for a bug report".
- The release zip ships `install.sh` and keeps `gimp-openfill.py`
  executable; unzipped on macOS or Linux, GIMP skipped the plug-in.
- `install.sh` writes the same minimal config as `install.ps1` (no
  outdated SD 1.5 and SDXL defaults), finds an existing ComfyUI and prints
  the current next steps.
- A cancelled or failed multi-round Generative Expand removes the layers
  earlier rounds added and restores the canvas.
- Windows: OpenFill detaches from the blank terminal window GIMP opens when
  it runs Python plug-ins, and its child processes run without a window.
- Installing custom nodes no longer fails when a folder on PATH cannot be read
  (WinError 448, for example a junction from a package manager); that folder
  is skipped while looking for git.
- Generative Fill with no selection regenerated the whole image; it now stops
  with "Nothing is selected ..." before starting the backend (`sel-none`).
- Transparent areas in the context were sent to the model as black; the
  context is now flattened over the mean visible colour
  (`img-alpha-transparent`, `img-text-layer`).
- A visible channel tinted the fill; the context is composited with channels
  hidden (`sel-channel-active`).
- Whole-image and edge-to-edge fills got a colour cast from colour matching
  without reference pixels; colour matching is skipped then
  (`sel-whole-image`).
- A pale band around fills, caused by the grown layer mask exposing the
  repainted context.
- Text to Image into a layer produced a layer at the model's working size
  instead of the selection's size (`mode-txt2img-layer`).
- Text to Image into a new image failed in batch mode (no display) and leaked
  the image (`mode-txt2img-image`).
- Indexed images passed in by scripts were filled and squeezed into the
  palette; they are now refused with a hint to convert to RGB.
- Small selections produced a blurry, semi-transparent blob and the removed
  object stayed visible: grow and feather now scale with the selection, the
  model-side mask is no longer blurred past full strength, tiny selections are
  upscaled, and LaMa/MAT get hard masks (`docs/evidence/small-selection/`).
- The Model choice ignored catalog ids when the plug-in was installed through
  a junction or symlink.
- An unreadable `config.json` stopped the plug-in; it is now moved aside as
  `config.json.corrupt-<time>` and defaults are used.
- A busy port produced a server log dump; start-up now names the program that
  holds the port, reports the real elapsed time and strips ANSI codes from log
  excerpts.
- The Settings dialog reports a failing save instead of losing it.
- A ComfyUI that crashed or was closed during a job produced a raw connection
  error; it now gives a plain message and the image is left unchanged
  (`rob-backend-crash-mid-job`).
- Two GIMP instances requesting a fill at the same moment started two
  supervisors and two ComfyUI servers, and the backend shut down when the
  first GIMP closed while the second was still generating
  (`rob-two-requests`, `rob-two-requests-first-gimp-exits`).
- Cancel while the backend was starting took effect only after about 23 s; it
  now stops within a few seconds (`rob-cancel-starting-backend`).
- Cancel while the result was downloading still inserted the layer
  (`rob-cancel-downloading`).
- Cancels were reported as errors with a traceback.
- A resumed download whose checksum failed stopped for good; it is now fetched
  once more from zero (`rob-download-corrupt-part`).
- Download network errors showed raw socket text; they now say what to check.
  Disk-space errors use readable sizes.
- Some uploads, such as the mask of a one-sided expansion, failed with
  "LoadImage: End of file": ComfyUI's PyAV-based image loader took the PNG
  for a video stream. Uploaded PNGs now carry a padding chunk.
- Removal with MAT came back soft and left ghosts of the removed object
  (`photo-*-remove`).
- Quality-tier fills painted a framed picture into the selection
  (`photo-fabric-replace`, `photo-landscape-replace`).
- One-sided Generative Expand came back as a flat colour bar
  (`photo-*-extend`).
- A GLib "Source ID was not found" warning when the progress window closed,
  and picker timers that fired after the picker had closed.
- HTTP error responses were not closed (ResourceWarning).
- Discard in the variations picker after Generative Expand left the canvas
  enlarged; it now restores the original size in the same undo step.
- The variations picker window could stay invisible inside GIMP on Windows
  (same cause as the progress window); it is now shown explicitly.
- The floating progress window was created but never marked visible by
  Windows inside a GUI GIMP session (screenshots taken with PrintWindow did
  not reveal it); it is now shown without taking focus.

### Security

- Catalog hardening from a security review: live-hub models get their own
  folders (no more clashes on equal file names, and no file is deleted that no
  entry owns), a download without a published SHA-256 asks first, damaged
  `hub-selected.json` / `hub-cache.json` files no longer break commands or
  discard results, Hugging Face files must be pinned to a commit, network
  errors (401, 429, 5xx, interrupted downloads,
  permission errors) get plain messages, and the model list no longer
  freezes the interface.
- docs/security.md states that there is no telemetry.

- The Hugging Face token is sent only to Hugging Face hosts over https, never
  forwarded on redirects, and redacted from logs; `config.json` is saved
  owner-only on POSIX.
- Catalog paths must be plain relative names and every catalog file needs a
  sha256 (LaMa gained one); downloads reject wrong resume offsets and
  oversize bodies and remove `.part` files on failure.
- Custom node installs validate the GitHub URL, folder and full commit and
  verify the checked-out commit before pip runs.
- Supervisor tasks are re-validated against the catalog and the config; pids
  from state files only count when they belong to a Python process.
- Multipart upload parameters are escaped.
- A stale supervisor start lock or GIMP watcher entry only counts while its
  pid belongs to a live Python or GIMP process.
- A resumed download that fails its sha256 check is discarded and never used.

### Known issues

- SD 1.5 (the Fast tier) often ignores the prompt on flat or featureless
  regions and on small selections and continues the background instead
  (`sd15-prompt-ablation`). Use the Quality tier (an SDXL model under
  Advanced > Model) for new content there.
- The Quality tier can return a flat tinted area instead of the requested
  content on some structured backgrounds (`photo-architecture-replace`).
- Generative Expand gets weaker when a side grows by more than 25 %; the
  editor shows a warning, and expanding in rounds gives the best results.
- Fine textures (fabric) come back softer in large fills and expansions,
  which are generated below 1:1 resolution.
- MAT cannot rebuild structured backgrounds such as window frames behind a
  removed object (`photo-architecture-remove`).
- The out-of-VRAM and disk-full paths are covered by unit tests only; neither
  could be reproduced end to end on the 8 GB development machine.
- Generative Expand's Discard removes the new layers but keeps the larger
  canvas; `Edit > Undo` twice restores it.
- AUTOMATIC1111, FLUX, Qwen and the Linux, macOS and Flatpak installs are
  not verified end to end.

## [2.0.0-alpha.1] - 2026-10-08

Complete rewrite for GIMP 3. Nothing from the 0.x code base runs under GIMP 3
(`gimpfu`, Python 2 and the old PDB calls are gone), so this is a new major
version that keeps the name, the license and the original author's credit.

### Added

- GIMP 3 plug-in (`Gimp.PlugIn`, libgimp 3 via GObject Introspection) with
  five procedures under `Filters > AI`: Generative Fill, Generative Expand,
  Text to Image, Generative Fill Settings, Stop AI Backend.
- Generative Fill from any selection: context crop, working-size scaling,
  mask grow/feather, result inserted as a new layer with a feathered layer
  mask; 1 to 8 variations in a layer group.
- Generative Expand (outpaint) by margins with seam pre-fill; the canvas only
  grows when generation succeeds.
- Text to Image into a new layer (selection-sized) or a new image.
- ComfyUI backend (primary): API-format workflow builders for SD 1.5 inpainting
  and SDXL + Fooocus inpaint patch, upload/queue/history/view client, stdlib
  websocket progress, readable error mapping (missing model, out of VRAM,
  timeout, rejected workflow).
- Managed backend lifecycle: detached supervisor process starts ComfyUI hidden
  (no console window), binds it to a Job Object, watches the GIMP pid, enforces
  an idle timeout, and exposes state through `server-state.json`.
- AUTOMATIC1111 backend behind the same `Backend` interface (untested on the
  development machine).
- Settings dialog persisted to `<profile>/stable-boy/config.json`, with model
  lists populated from the server.
- `install.ps1` / `install.sh`, unit tests (`tests/`), end-to-end batch script
  (`tests/gimp_batch_fill.py`), `docs/design.md`, `docs/models.md`,
  `docs/evidence/`.

### Changed

- Inpainting no longer needs a layer named `Inpainting Mask`; the selection is
  the mask.
- Results are always layers with masks (the old "Images" target survives only
  in Text to Image).

### Removed

- GIMP 2.10 / Python 2 sources (`src/`), the X/Y plot script, the upscale
  command and the img2img command. Image to Image is covered by Generative
  Fill with a strength below 1.0.

## [0.4] - 2023

Last GIMP 2.10 release by Torben Giesselmann. Script support (X/Y plot),
inpainting mask applied to new layers, shared preferences dialog, command
pattern refactoring.

## [0.3.3] / [0.3.2] / [0.3.1] / [0.3] - 2022-12

Timeout handling, progress bar, sampler list updates, automatic inpainting
area detection, upscaler settings.

## [0.2] - 2022-12-04

Rectangular selections as processing region; Windows startup fix.

## [0.1] - 2022-12-02

Initial release: text to image, image to image, inpainting, upscaling via the
AUTOMATIC1111 WebUI API.

[Unreleased]: https://github.com/beckettharriman/openfill/compare/v2.0.0-beta.4...HEAD
[2.0.0-beta.4]: https://github.com/beckettharriman/openfill/releases/tag/v2.0.0-beta.4
[2.0.0-beta.3]: https://github.com/beckettharriman/openfill/releases/tag/v2.0.0-beta.3
[2.0.0-beta.2]: https://github.com/beckettharriman/openfill/releases/tag/v2.0.0-beta.2
[2.0.0-beta.1]: https://github.com/beckettharriman/openfill/releases/tag/v2.0.0-beta.1
