# Shared models: use the models of another ComfyUI, read-only (issue #6)

Status: option A built on `feat/issue-6-shared-models`, stacked on the issue #8 fix
(`fix/issue-8-companion-encoders`). The rest of this note is the design as written
before building; where the build differs, "As built" below wins.

## As built

- **Setting**: `shared_models` in config.json, one string, folders separated by
  `os.pathsep` (or new lines), `shared_models.parse`/`join`. No separate list widget
  and no one-time "Use Krita's models?" offer: the field has **Add folder...** and
  **Find Krita AI Diffusion** (detection) buttons instead, in GIMP's Settings >
  General and the PhotoCraft companion's Engine view (page and Tk panel).
- **Where a file is**: instead of a `ModelRoots` value threaded through every
  catalog function, a `FileSpec` gets `root` (the shared folder it was found in, ""
  for OpenFill's own). `catalog.spec_path` joins it; `file_installed`,
  `missing_previews` and `install_status` use it, so every caller works unchanged.
  `local_models.build(entries, models_root, base, shared)` is the one place that
  rewrites the catalog (app, companion rescan, supervisor, GIMP model menu):
  `share()` adopts catalog files found in a shared folder (same name and size, its
  folder or the alias) and, for FLUX, resolves T5/CLIP-L/VAE by role across own and
  shared folders; `scan()` finds own model files in shared folders too (ids keyed
  by root, so they never collide with OpenFill's own).
- **Bundled entries** now get the issue #8 companion resolution too, with or without
  shared folders (a bundled FLUX entry reuses a `text_encoders/` T5 of OpenFill's own
  ComfyUI as well). `ModelEntry.checksum_known` only counts files that would be
  downloaded.
- **Order**: OpenFill's own folder, then the shared roots in the setting's order. A
  shared file whose loader name an earlier folder already has is not counted
  (ComfyUI would load the other one). One ComfyUI detail found while building:
  comfyui-inpaint-nodes registers `inpaint` after the yaml is read and appends
  ComfyUI's own `models/inpaint` after the extra paths, so the yaml starts with an
  `openfill_own` section naming OpenFill's own `models/inpaint` when a shared root
  has an `inpaint` folder; the node then finds its path already listed.
- **Guards at the trust point** (supervisor): a download task for a file that has a
  `root` or no download address is refused; a download destination inside a shared
  root is refused; delete skips files with a `root`, files found in place, and any
  path inside a shared root. Delete also joins every path under OpenFill's own models
  folder, so a shared file is out of its reach in the first place.
- **Restart**: changing the list asks a running backend to stop (App.set_shared_models,
  GIMP's Settings save); the next job starts it with the new yaml.
- **Not built**: B (using the other server as is) and the "N usable models found" count
  per folder.
- **After review (2026-11-06)**:
  - The setting is stored one folder per line (`join`). A typed single line is split on
    the list separator, keeping existing folders whole.
  - `check()` refuses relative, missing or own-models folders (also through a link) in
    GIMP, the page and the Tk panel. The page saves all settings or none.
  - Guards compare real paths (`blocked`, `guard_roots`). A link from OpenFill's models
    folder into a shared one makes it read only, while a shared parent of OpenFill's
    folder leaves OpenFill's own part writable.
  - Saving the list no longer stops the engine. The supervisor records the yaml it
    started with (`shared_models` in server-state.json), and `App.backend` restarts the
    engine before the next job only when nothing runs. The yaml is also written on save,
    for a ComfyUI started by hand.
  - The yaml is written with `ensure_ascii=False`. The `scan()` shadow slice is fixed.
    Files a catalog entry takes from an alias folder are not listed again as your own.
  - Bundled FLUX entries take a found T5, CLIP-L or VAE only up to 1.2 times the
    catalog file's size; the user's own FLUX file takes any compatible one.
- **After the second review (2026-11-06)**:
  - Folders are compared by identity (`os.path.samefile`, device and inode of each
    existing parent), so bind mounts, second mounts and case differences count.
  - OpenFill's folder is refused only when it is an alias (link or mount) of a folder
    inside the other install. A root that is OpenFill's folder through the other's
    extra_model_paths.yaml is skipped, and it is not guarded either.
  - An unchanged list is not checked again (an unplugged drive does not block saving).
    Web and Tk check the list against the ComfyUI folder saved with it.
  - Clearing the list keeps the yaml as `{}`. Names that are not UTF-8 are refused, and
    are never written. The supervisor and the app catch UnicodeError.
  - The supervisor records the roots it started with (`shared_roots`). While the running
    engine has an older list, the catalog uses that list, and a job for a model only the
    new list has says it waits for the restart.
  - The restart is skipped when the supervisor is gone, and it waits for the state file
    to go.
  - The yaml is rewritten when the ComfyUI folder changes and when the supervisor
    restarts after a node install.

## Problem

People who already run ComfyUI for something else, most often the managed server of
Acly's Krita AI Diffusion plug-in, have gigabytes of models OpenFill could use:
RealVisXL, the Fooocus inpaint patch, MAT, CLIP-L, the T5 GGUF and the FLUX VAE. They
ask (#6, and the Reddit thread it links) to point OpenFill at those models without
downloading them again, and without OpenFill changing that other install.

Today OpenFill can only use another ComfyUI by making it *its* ComfyUI (`comfy_dir`),
and then it writes into it. Everything it writes, and when:

| What | Where it goes | When | Code |
|---|---|---|---|
| `openfill-nodes` node pack (copied, replaced when its VERSION differs) | `<comfy>/custom_nodes/openfill-nodes` | every backend start, automatically | `supervisor._run_backend` -> `downloads.ensure_bundled_nodes` (supervisor.py ~line 960); also `App._put_bundled_nodes` (app.py ~276) before a run that needs it |
| `comfyui-inpaint-nodes`, `ComfyUI-GGUF` (git clone at a pinned commit, their requirements via pip) | `<comfy>/custom_nodes/`, `<comfy>`'s Python | Models tab, Install nodes | `App.queue_node_install` -> `supervisor._install_nodes` -> `downloads.install_custom_node` |
| `opencv-python-headless` (pinned in `comfy_setup.json`) | `<comfy>`'s Python | after Install nodes, and **automatically** after a fill that fell back to the neutral pre-fill | `supervisor._ensure_opencv`, `App._after_generation` -> `queue_opencv_install` |
| Model files | `<comfy>/models/...` | Models tab, Download | `supervisor._download` |
| Live-preview decoders (`taesd*_decoder.pth`, `taef1_decoder.pth`) | `<comfy>/models/vae_approx` | **automatically** before a run whose decoder is missing | `App._fetch_missing_previews` (app.py ~297) |
| Model deletion | `<comfy>/models/...` | Models tab, Delete downloaded files | `supervisor._remove` |
| ComfyUI's own output: results, uploads, temp, `user/` (settings, logs, database) | `<comfy>/output`, `input`, `temp`, `user` | every run: OpenFill starts ComfyUI from that folder without directory flags | `supervisor._run_backend` command line |

So "point OpenFill at my Krita ComfyUI" today means: a node pack copied in on every
start, maybe nodes and OpenCV installed into Krita's venv, previews downloaded into its
models folder, and OpenFill's results and uploads in its `output`/`input`. Krita's
own updater moves `ComfyUI/` aside and reinstalls it on an upgrade (`Server.upgrade`,
server.py ~377), so anything OpenFill put in there can also vanish under it.

## How Krita AI Diffusion lays out its managed ComfyUI

Read from Acly/krita-ai-diffusion at `828b70c` (`ai_diffusion/util.py`,
`settings.py`, `backend/server.py`, `backend/resources.py`, `presets/models.json`,
docs `common-issues.mdx`):

- User data folder (`util._get_user_data_dir`): Qt's AppDataLocation for Krita plus
  `ai_diffusion`, so
  - Windows `%APPDATA%\krita\ai_diffusion`
  - Linux `$XDG_DATA_HOME/krita/ai_diffusion` (default `~/.local/share/krita/ai_diffusion`);
    Flatpak Krita: `~/.var/app/org.kde.krita/data/krita/ai_diffusion`
  - macOS `~/Library/Application Support/krita/ai_diffusion`
  - fallback when Qt's folder is not Krita's: GenericDataLocation + `krita-ai-diffusion`.
- `settings.json` in that folder (JSON, may hold comments: Krita reads it with
  `read_json_with_comments`). Keys that matter: `server_mode` (`managed`, `external`,
  `cloud`), `server_path` (default `<user data>/server`, the user can move it),
  `server_url`.
- Managed server at `server_path`:
  - `ComfyUI/` (has `main.py`, `nodes.py`, `custom_nodes/`), `venv/` (uv-made), `uv/`, `.cache/`,
    `.version` (`<plugin version> <backend>`).
  - `models/` next to `ComfyUI/`, made by `_create_extra_model_dirs`: `checkpoints`,
    `clip_vision`, `controlnet`, `diffusion_models`, `inpaint`, `ipadapter`, `loras`,
    `model_patches`, `style_models`, `text_encoders`, `upscale_models`, `vae`.
  - Krita appends a `krita-managed:` section to `ComfyUI/extra_model_paths.yaml` with
    `base_path: ../models` and those folders (plus `clip`, `unet`), so its ComfyUI
    reads both `server/models` and `server/ComfyUI/models` (older installs kept models
    there; Krita checks both, `Resource.exists_in(self.path)` and `exists_in(self.comfy_dir)`).
- Custom nodes it installs: `comfyui_controlnet_aux`, `ComfyUI_IPAdapter_plus`,
  `comfyui-tooling-nodes`, `comfyui-inpaint-nodes` (pinned `bd6d8fd`, OpenFill pins
  `5b76817`); `ComfyUI-GGUF` is optional.
- Files a Krita user is likely to have that OpenFill's catalog lists or can reuse:

| Krita path (under `server/`) | OpenFill |
|---|---|
| `models/checkpoints/RealVisXL_V5.0_fp16.safetensors` | `realvisxl-v5`, same file name in `checkpoints/` |
| `models/inpaint/fooocus_inpaint_head.pth`, `inpaint_v26.fooocus.patch` | `fooocus-inpaint-patch`, same names and folder |
| `models/inpaint/MAT_Places512_G_fp16.safetensors` | `mat-places512`, same |
| `models/text_encoders/clip_l.safetensors` | FLUX CLIP-L (catalog: `clip/`, the alias folder) |
| `models/text_encoders/t5-v1_1-xxl-encoder-Q5_K_M.gguf` | FLUX T5 (catalog: `clip/`, same file name) |
| `models/vae/flux_vae.safetensors` | FLUX VAE (catalog: `vae/ae.safetensors`, other name) |
| `models/checkpoints/dreamshaper_8.safetensors` | not usable: plain SD 1.5, OpenFill needs the inpainting one |
| `models/vae/flux2-vae.safetensors` | not usable for FLUX.1 (32 latent channels) |

  Krita has no FLUX.1 Fill and no LaMa, so a Krita user still downloads those from
  OpenFill; with this design, only the FLUX UNet itself, not the encoders or VAE.

Note the last three rows of the first table: the T5, CLIP-L and VAE are exactly the
case issue #8 fixed for the user's own FLUX file, but bundled entries still look only
at the catalog path. Shared models need that resolution for bundled entries too (build
step 3).

## ComfyUI's extra_model_paths mechanics

From ComfyUI (v0.40.0 here, `main.py` `apply_custom_paths`, `utils/extra_config.py`,
`folder_paths.py`):

- `<comfy>/extra_model_paths.yaml` is loaded if it exists, then every file given with
  `--extra-model-paths-config PATH [PATH ...]` (repeatable, `nargs='+'`,
  `action='append'`). A file passed on the command line is never written to and does
  not have to be inside the ComfyUI folder: **nothing needs to go into the other install.**
- Format: top-level sections; each has optional `base_path` (expanded with
  `expanduser` and `expandvars`, relative to the yaml's folder), optional
  `is_default` (puts the paths first), and `folder_name: path` entries; several paths
  per folder are separated by newlines. Paths without `base_path` are taken as given
  (relative ones against the yaml's folder) and are **not** run through `expandvars`.
- Folder names go through `map_legacy` (`clip` -> `text_encoders`, `unet` ->
  `diffusion_models`), and extra paths are appended after ComfyUI's own
  `models/<folder>` unless `is_default`. For a duplicate file name the first folder in
  the list wins.
- Custom node packs register their own folder names (`inpaint` by
  comfyui-inpaint-nodes, `unet_gguf`/`clip_gguf` by ComfyUI-GGUF, which reuse the
  `diffusion_models`/`text_encoders` lists at import). A yaml key for a folder that no
  loaded node registers is harmless.
- `--output-directory`, `--input-directory`, `--temp-directory`, `--user-directory`
  move everything ComfyUI writes at run time (`--user-directory` must exist).
  `--base-directory` moves all of them plus `models` and `custom_nodes`.

## Options

### A. OpenFill's own ComfyUI, reading the other install's models through a yaml it owns

OpenFill keeps running its own ComfyUI (the one Download ComfyUI set up, or one the
user picked that is theirs to change). The user adds one or more **shared model
folders**. OpenFill writes `<profile>/openfill/shared-model-paths.yaml` and starts
ComfyUI with `--extra-model-paths-config <that file>`. OpenFill's catalog checks,
local-model scan and companion resolution look in those folders too, read-only;
downloads, deletions and preview decoders only ever go to OpenFill's own `models/`.

- Writes to the other install: none, by construction (OpenFill never builds a path
  into it except to read).
- Works with any other ComfyUI, any version, any node set: only files are read.
- Costs a separate ComfyUI (about 2 GB for the Windows portable; on Linux and macOS
  the user's own checkout). Both servers can run, but not usefully on one 8 GB card at
  the same time; OpenFill's idle timeout already stops its server.

### B. Use the other ComfyUI server as it is, with only the workflows its nodes support

OpenFill connects to (or starts) the other ComfyUI without changing it: no node copy,
no installs, no downloads into it, results and uploads redirected.

- B1, connect: the user starts Krita's server; OpenFill talks to `server_url`. Already
  half possible today (`url` plus autostart off), but OpenFill still offers Install
  nodes, queues OpenCV, fetches previews and computes models from `comfy_dir`.
- B2, start it: OpenFill runs the other install's `main.py` with its own Python,
  `-B` (no `.pyc` writes; `PYTHONDONTWRITEBYTECODE=1`), the four directory flags
  pointed into OpenFill's profile, and no `ensure_bundled_nodes`.
- Model and workflow availability comes from the server (`/object_info`): Fooocus
  only with `INPAINT_LoadFooocusInpaint`, GGUF models only with `UnetLoaderGGUF`,
  native-resolution LaMa only with `openfill-nodes` (else the regular LaMa path,
  which needs `comfyui-inpaint-nodes`), colour match only with `INPAINT_ColorMatch`,
  pre-fill modes only with OpenCV in that Python.
- Downside: OpenFill's quality depends on someone else's node versions (Krita pins
  inpaint-nodes `bd6d8fd`, OpenFill `5b76817`), ComfyUI version and PyTorch; any of
  them can change under OpenFill on a Krita update. Krita's server is not running
  when Krita is closed (B1), and two programs managing one server's lifetime is
  fragile (B2: Krita may try to start it on the same port). Downloads OpenFill-only
  models (LaMa, FLUX Fill) still need somewhere to go: a second, OpenFill-owned
  models folder, which B2 can add with `--extra-model-paths-config` like A.

### C. Others considered

- C1, link the files: create symlinks or hard links in OpenFill's `models/` to the
  other install's files. Rejected: Windows symlinks need Developer Mode, hard links
  only work on one drive, links break silently when Krita updates or moves a model,
  and the Delete button would have to learn links.
- C2, edit the other install's `extra_model_paths.yaml` (what Krita itself does to
  ComfyUI). Rejected: it is a write into the other install, and Krita rewrites that
  file on upgrade.
- C3, `--base-directory` on the other install's code. Rejected: moves
  `custom_nodes` too, so the other install's nodes vanish; no gain over A.
- C4, copy the models. Rejected: the point is not to have two copies.

## Recommendation

Build **A**. It answers the request (use the models, write nothing) with no new
dependency on someone else's ComfyUI, nodes or versions, and it reuses machinery that
exists: OpenFill already starts its own ComfyUI with a command line it controls, and
issue #8 added companion resolution by role. B1 stays possible as it is today for
people who want one server; a later "read-only server" switch (B with every write
path above turned off and features gated on `/object_info`) is a separate issue and
not part of this build.

## UX

Settings > Models (GIMP) and Engine settings (PhotoCraft companion), a new group
"Models from another ComfyUI":

- Read-only list of shared folders. Each row: a label ("Krita AI Diffusion", or the
  folder name), the path, and "N usable models found" (catalog entries now complete
  plus own files recognised there), or the reason none are ("no models OpenFill can
  use in this folder").
- When Krita's managed server is detected and not yet added: one line "Krita AI
  Diffusion's models were found at <path>. Use them?" with **Use** and **No thanks**
  (remembered, so it is asked once).
- **Add folder...** opens a folder chooser. Accepted: a ComfyUI folder (has
  `models/`), a `models` folder, or Krita's `server` folder. OpenFill works out the
  model roots (below) and says which it took.
- **Remove** next to each row; never touches files.
- Under the list, plain: "OpenFill only reads these folders. Downloads go to your
  OpenFill ComfyUI's models folder."
- Models tab rows whose files are all or partly in a shared folder show
  "in Krita AI Diffusion's folder" (like today's "in your ComfyUI folder" for local
  files); their menu has no Delete downloaded files, and Download only fetches what is
  missing, into OpenFill's own folder.
- Changing the list restarts OpenFill's ComfyUI when it is running (it reads model
  folders at start), the same way Install nodes does.

Not in this build: a picker for which models to use. Everything usable in a shared
folder shows up; the active model is chosen as today.

## Model roots of a shared folder

From the folder the user picked (or detection found), in this order:

1. Krita managed server (`<p>/ComfyUI/main.py` and `<p>/models` exist): `<p>/models`,
   then `<p>/ComfyUI/models`.
2. A ComfyUI folder (`comfy_layout` or `main.py` + `models/`): `<p>/models` (portable:
   `<p>/ComfyUI/models`), plus the roots in its own `extra_model_paths.yaml`, read
   (not written) with a minimal parser for the two-level `section: key: path` form
   only (OpenFill is stdlib-only, no PyYAML); anything else in that file is skipped
   with a log line.
3. Otherwise the folder itself is a models folder.

Folder names read under each root: `checkpoints`, `unet`, `diffusion_models`, `clip`,
`text_encoders`, `vae`, `inpaint`, `loras`, `vae_approx`. They go into the yaml under
those names; ComfyUI maps the legacy ones.

## The yaml OpenFill writes

`<profile>/openfill/shared-model-paths.yaml`, regenerated from settings on every
backend start, one section per root, absolute paths, no `base_path`, each path
written as a JSON string (valid YAML double-quoted scalar, so `C:\`, `#`, `:` and
spaces are safe; no `base_path` so `expandvars` never touches a `$` in a path):

```yaml
openfill_shared_1:
  checkpoints: "/home/me/.local/share/krita/ai_diffusion/server/models/checkpoints"
  text_encoders: "/home/me/.local/share/krita/ai_diffusion/server/models/text_encoders"
  ...
```

Only folders that exist are listed. Not `is_default`: OpenFill's own `models/` stays
first, so for two files with the same loader name ComfyUI loads OpenFill's, and
OpenFill's resolution uses the same order.

## Guarantees

1. Nothing is written into a shared root, ever: no downloads, no deletions, no
   preview decoders, no caches, no yaml. Enforced at the trust point: the supervisor
   checks every download/remove destination against the configured own models root
   (already true: `_checked_models_root`), and remove refuses any file that resolves
   into a shared root even if a task names it.
2. Nothing is written into the other ComfyUI's program folder (`custom_nodes`, venv,
   `output`, `input`, `temp`, `user`): option A never starts it and never runs its
   Python.
3. OpenFill keeps working when a shared folder disappears (drive unplugged, Krita
   uninstalled): its models read as not installed again, nothing else changes, and
   ComfyUI starts with a yaml naming only existing folders.

How tests prove 1 and 2: a fixture "other install" tree (Krita layout: `server/ComfyUI`
with `main.py`, `custom_nodes/`, `extra_model_paths.yaml`; `server/models/...` with
small fake model headers). Before and after each scenario, snapshot every path under
it: type, size, `st_mtime_ns`, mode, link target, and SHA-256 of every file (they are
tiny); also the directory listing, so a new file shows. Assert equal. atime is left
out (reads change it). Scenarios: detection, settings change, catalog load and local
scan, install status, Download of a model whose companions are shared, Delete
downloaded files of a model partly in the shared root (task naming shared paths
directly too), preview decoder fetch, building the backend command line and yaml.
On Linux and macOS the same suite runs a second time with the fixture made read-only
(`chmod -R a-w`, skipped as root), so any write raises instead of slipping past a
snapshot. A real e2e on Linux does the same with a bind-mounted read-only copy of a
Krita-like tree and one FLUX Fill and one SDXL fill through a real ComfyUI.

## Detection of Krita

`shared_models.detect_krita(env, home)`, never raises, returns candidates like
`comfy_setup.detect_comfy`:

1. User data folders in this order: Windows `%APPDATA%\krita\ai_diffusion`; macOS
   `~/Library/Application Support/krita/ai_diffusion`; Linux
   `$XDG_DATA_HOME/krita/ai_diffusion` (default `~/.local/share/...`), Flatpak
   `~/.var/app/org.kde.krita/data/krita/ai_diffusion`; then each platform's
   `krita-ai-diffusion` fallback.
2. In each: read `settings.json` with `//` comment lines removed. `server_path`, if
   set (Krita reads a relative one against its plug-in's `backend` folder; skip those, rare),
   else `<user data>/server`.
3. Accept when `<server>/ComfyUI/main.py` exists and `<server>/models` is a folder.
   Show `.version` in the label if present.
4. `server_mode` `external`: the user's own ComfyUI; offer `server_url`'s machine only
   if it is a local folder we can find through `detect_comfy`. `cloud`: nothing.

`detect_comfy` candidates that are not OpenFill's `comfy_dir` are offered the same way
("ComfyUI at <path>").

## Edge cases

- **Different ComfyUI versions**: no issue in A. OpenFill's ComfyUI only reads the
  files; folder names are written in the yaml under both modern and legacy names.
- **Missing GGUF node**: in A it is OpenFill's ComfyUI that needs it, as today
  (Install nodes into OpenFill's own ComfyUI). A GGUF file in a shared folder is fine.
- **Folder name differences**: `clip`/`text_encoders` and `unet`/`diffusion_models`
  are aliases (the #8 resolution already searches both); Krita's FLUX VAE is
  `flux_vae.safetensors` (accepted by `local_models.compatible`); `inpaint` is a
  custom folder, registered by comfyui-inpaint-nodes in OpenFill's ComfyUI.
- **Same file in both places**: OpenFill's own wins (yaml not `is_default`, and
  resolution checks the own root first). A catalog file in a shared root with another
  size than the catalog says is treated like today (not that catalog file), but may
  still be picked up by companion resolution or the local scan.
- **Loader name collision with different content** (`text_encoders/clip_l.safetensors`
  in both, different files): ComfyUI loads the first; OpenFill shows the one it will
  load.
- **Subfolders**: ComfyUI lists files recursively and names them `sub/file`; the
  local scan and companion resolution already use the path below the model folder.
  `Path.rglob` does not follow directory symlinks, ComfyUI does; a Krita user with
  symlinked folders gets those models only by adding the link target. Documented.
- **Local model ids**: today `local-<sha1(rel)>`. With several roots the id must
  include the root (`local-<sha1(root-tag + rel)>`), else two roots with
  `unet/x.gguf` collide; the own root keeps today's ids so "Set active" survives.
- **Slow or network folders**: the scan reads headers only, cached by path, size and
  mtime as today; a root that is not reachable is skipped with a log line and the
  row says "not found".
- **Krita updates while OpenFill runs**: files can vanish mid-run; the job fails
  with ComfyUI's missing-file error and the next catalog refresh shows the model as not
  installed. No retry logic.
- **Gated or checksum-verified catalog files found in a shared root**: used by name
  and size, like files already in OpenFill's own folder today; never re-hashed.
- **Windows paths**: long paths (the existing `windows_path_problem` check applies to
  the yaml path too) and paths on other drives (fine, yaml takes absolute paths).

## Test plan

Unit (stdlib, in `tests/`):

- `test_shared_models.py`: Krita detection per platform from fake env/home trees
  (default location, moved `server_path`, comments in `settings.json`, Flatpak,
  `external` and `cloud` modes, missing `main.py`); model roots of a picked folder
  (Krita server, portable, checkout with its own `extra_model_paths.yaml` including the
  minimal parser's refusals, bare models folder); yaml text (quoting of `C:\`, `#`,
  spaces, `$HOME` kept literal, only existing folders, one section per root) and a
  round trip through ComfyUI's own loader in the e2e.
- `test_catalog.py`: `install_status`, `missing_previews`, `installed_stand_in`,
  `runnable_installed` with a file only in a shared root, only in the own root, in both.
- `test_local_models.py`: scan over two roots, ids, companion resolution across roots
  in the documented order; bundled FLUX entries resolving T5/CLIP-L/VAE from the
  Krita layout (only the UNet left to download).
- `test_downloads.py` (supervisor): download and remove tasks naming a shared path
  are refused; Delete of an entry partly in a shared root removes only own files;
  the backend command line has `--extra-model-paths-config` exactly when shared roots
  exist.
- The no-write snapshot suite above, normal and read-only.
- Companion (`photocraft/tests/test_companion.py`): the settings API accepts and
  validates the list; the panel shows the detection line and rows.

Real (manual or `photocraft/e2e`): a Krita-like tree (built with symlinks to real
models, made read-only with a bind mount), OpenFill's ComfyUI started by the
supervisor; RealVisXL fill with the shared Fooocus patch, MAT removal, FLUX Fill with
shared T5/CLIP-L/VAE and an own UNet; snapshot equality of the tree afterwards; and
the same on Windows with a real Krita install (I don't have one here; needs a tester).

## Build plan, file by file

1. `gimp-openfill/openfill/config.py`: `Settings.shared_model_dirs: list[str]` (folders
   the user added) and `krita_offer_dismissed: bool`. Loader keeps unknown types out.
2. `gimp-openfill/openfill/shared_models.py` (new, GIMP-free): `detect_krita`,
   `model_roots(folder) -> list[Root]` (path + label), the minimal yaml reader,
   `yaml_text(roots) -> str`, `write_yaml(base, roots) -> Path | None`.
3. `gimp-openfill/openfill/catalog.py`: a `ModelRoots` value (own root first, then
   shared, read-only flag per root) accepted wherever a `models_root` is today
   (`file_installed`, `install_status`, `missing_previews`, `installed_stand_in`,
   `runnable_installed`, `gated_files_missing`, `model_spec_for`); a `Path` keeps
   working, so callers move one by one. `file_installed` also tries the alias folder.
4. `gimp-openfill/openfill/local_models.py`: `scan` over all roots (ids per root);
   `resolve_companion` across all roots in order; a `with_companions(entry, roots)`
   applied to bundled and live FLUX entries too, so a Krita T5/CLIP-L/VAE counts and
   only missing files are downloaded (the found ones lose their URL, like #8's).
5. `gimp-openfill/openfill/supervisor.py`: `_run_backend` adds
   `--extra-model-paths-config <base>/shared-model-paths.yaml` when there are shared
   roots (yaml written right before start from config.json); `_catalog` passes the
   roots; `_remove` and `_download` refuse destinations inside any shared root.
6. `gimp-openfill/openfill/app.py`: catalog load and `rescan`, `remove_model`
   (own-root files only), `_fetch_missing_previews` (only into the own root; a decoder
   in a shared `vae_approx` counts), `model_rows` (row origin "shared:<label>"),
   `set_shared_dirs()` which saves, rescans and restarts a running backend.
7. `gimp-openfill/openfill/catalog.py` view functions + `ui_text.py`: the source
   label, the "only reads" line, no Delete for shared rows.
8. `gimp-openfill/openfill/models_ui.py` and `gimp-openfill/gimp-openfill.py`: the
   group in Settings > Models (list, Add folder..., Remove, Krita offer).
9. `photocraft/openfill_photocraft/web.py` (`EDITABLE` gets the list, validated: each
   an existing absolute folder), `panels.py`, `static/` page: the same group.
10. `gimp-openfill/openfill/server.py`: nothing to add to the supervisor's command
    line (the supervisor reads config.json itself); restart helper reused.
11. Docs: a short "Use models from Krita AI Diffusion or another ComfyUI" section in
    `docs/models.md` and a CHANGELOG line; README and `docs/install.md` wording left to
    Beckett.
12. Tests as in the test plan, written first per step.

Order: 2, 3, 4 with their tests (no UI, no behaviour change without settings), then 5
and 6, then the UI (7 to 9), then the e2e.
