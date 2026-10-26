# Security notes

OpenFill is a GIMP plug-in that starts a local ComfyUI server, downloads
model weights and installs ComfyUI custom node packs. This page describes what
it trusts, what it checks, where it keeps data and how to report a problem.

## Threat model

Trusted: the user, their GIMP profile folder, the ComfyUI install they point
the plug-in at, the shipped `models/catalog.json`, and the Python interpreters
of GIMP and ComfyUI. Anything that can already write to those can run code as
the user, so the plug-in does not try to defend against it.

Not trusted, and checked:

- **The network.** Model files and custom node repositories come from Hugging
  Face, Civitai and GitHub over https. A file of the shipped catalog is only
  accepted when its size and SHA-256 match; a custom node is only installed at
  the commit the catalog pins. Live hub models (see "Live model search") follow
  the hash policy below.
- **Task and state files written while the plug-in is not looking.** The
  plug-in and its supervisor talk through JSON files in the profile
  (`tasks/*.json`, `server-state.json`, `tasks-status.json`). The supervisor
  re-validates every task against the catalog and `config.json` instead of
  acting on what the task says, and treats pids from state files with
  suspicion (see below).
- **The ComfyUI server's responses.** WebSocket frames are size-limited and
  preview images are parsed defensively; a misbehaving server can fail a
  generation but should not exhaust memory.

Out of scope: a malicious model checkpoint or custom node pack that the
catalog itself lists (pinning protects against later changes upstream, not
against a bad pick), ComfyUI's own security, and other users on a shared
machine who can read your home folder.

## What is validated

| Input | Check | Code |
| --- | --- | --- |
| Catalog file URLs | https only | `catalog.parse_catalog` |
| Catalog `dest` paths | relative, plain names only: no `..`, no drive letter, no absolute or UNC path; must end in a plain file name; checked when the catalog is parsed and again when a task is run, where the joined path must also resolve inside the models folder | `downloads.safe_relative_path`, `downloads.safe_join` |
| Catalog sha256 | 64 lowercase hex characters; every shipped file has one (unit test) | `catalog.parse_catalog`, `tests/test_catalog.py` |
| Custom node source | `https://github.com/<owner>/<name>`, folder is a plain name, commit is a full 40-character SHA | `downloads.validate_node_spec` |
| Download task | `model_id` must be a catalog entry; each file's URL and destination must belong to it; size, sha256, `gated` and `optional` are taken from the catalog, never from the task; the models folder must be the one of the configured ComfyUI | `supervisor.TaskWorker.checked_download_files` |
| Node install task | the custom_nodes folder and the Python interpreter must be the configured ComfyUI's; repo, folder and commit must match a catalog entry exactly | `supervisor.TaskWorker.checked_node_install` |
| Remove task | only files of the named catalog entry, inside the configured models folder | `supervisor.TaskWorker._remove` |
| Live (hub) entries | source is huggingface or civitai; the URL must be an https file URL on those hosts (Hugging Face URLs pinned to a commit); the main file is a .safetensors or .gguf with a known size, in its own folder `checkpoints/<id>/` or `unet/<id>/` so two models never share a file; companions and custom nodes must be copied verbatim from the shipped list; a malformed record is dropped, not fatal | `catalog._live_checks`, `catalog.parse_live_entries` |
| Existing files | a live download never deletes or overwrites a file that is already at its destination and does not match; it stops and tells you to move it. Removing a model deletes only the files of that catalog entry | `downloads.download_file`, `supervisor.TaskWorker._remove` |
| Downloaded data | written to `<file>.part`; Content-Length and the catalog size must agree before writing; a body longer than the catalog size is cut off and deleted; a resumed range must start at the expected byte; free disk space checked first; SHA-256 verified before the atomic rename onto the final name; a hash mismatch deletes the `.part` file | `downloads.download_file` |
| Custom node install | new installs use the GitHub zip of the pinned commit: its comment must equal the commit, unsafe member paths and symbolic links are refused. An existing git clone is checked out at the pin and `git rev-parse HEAD` must equal it before pip runs | `downloads.install_node_from_zip`, `downloads.install_custom_node` |
| ComfyUI setup manifest | the archive URL must be on the official ComfyUI GitHub releases, the name and folder plain names, the SHA-256 64 hex digits, pip packages pinned with `==`, nodes pinned like catalog nodes; the setup task's install folder must be absolute | `comfy_setup.parse_manifest`, `supervisor.TaskWorker._setup_comfy` |
| Pids in state files | a pid only counts as a live supervisor or task worker when it is alive and its process image name (when it can be read) is a Python interpreter; the watched GIMP pid counts as gone when its image name changes (pid reuse), and other GIMPs registered for the same profile keep the backend alive | `supervisor.pid_is_python`, `supervisor.run_backend` |
| WebSocket frames | at most 256 MB per frame; the client id is URL-encoded | `websocket.WebSocket`, `websocket.ws_url` |

The plug-in never kills a process by a pid read from a file. The supervisor
only terminates the ComfyUI child it started itself (a Windows Job Object with
kill-on-close ties that child to the supervisor). Filters > OpenFill > Stop AI
Backend writes a stop request into `server-state.json`
(`server.ServerManager.request_stop`); the supervisor acts on it.

Every subprocess is started with an argument list, never through a shell:
the supervisor and ComfyUI (`server.py`, `supervisor.py`), pip for node installs and
Windows' `tar.exe` for unpacking ComfyUI (`downloads.py`, `comfy_setup.py`; git only for resetting an existing clone), and the file manager for "Open models folder"
(`app.py`). Both installers quote every path; `install.ps1 -Model` validates
the catalog destinations, URLs and node pins the same way and downloads the pinned GitHub zip of each node with
curl as separate arguments.

## The Hugging Face token

- Stored in one place only: `config.json` in the plug-in's settings folder
  (below). On Linux and macOS the file is written with mode 0600.
- Never logged: `Settings` leaves the token out of its `repr()`, and both the
  plug-in log and the supervisor log pass every message through a filter that
  replaces anything that looks like a token (`hf_...`, `Bearer ...`,
  `"hf_token": ...`) with `[redacted]`.
- Never written anywhere else: the models cache, the task files, the status
  file and the debug dumps do not contain it (unit test in
  `tests/test_config.py`). The supervisor reads it from `config.json` only
  when a gated file is downloaded.
- Never put in a URL. It is sent only as an `Authorization: Bearer` header,
  only over https, only to `huggingface.co` and `hf.co` hosts (and their
  subdomains), and never forwarded on a redirect: Hugging Face redirects
  downloads to pre-signed CDN URLs that need no token (unit tests in
  `tests/test_downloads.py`).
- `install.ps1 -Model` reads it from the `HF_TOKEN` environment variable and
  hands the header to curl on standard input, so it does not show up in the
  process list. `tests/check_catalog_urls.py` does the same.

To remove the token, clear the field in the Models tab and save, or delete the
`hf_token` value from `config.json`. Revoke it at
https://huggingface.co/settings/tokens if you think it leaked.

## What is stored where

All plug-in state lives in `<GIMP profile>/openfill/` (for example
`%APPDATA%\GIMP\3.2\openfill` on Windows, `~/.config/GIMP/3.2/openfill` on
Linux):

| File | Contents |
| --- | --- |
| `config.json` | settings, including the Hugging Face token |
| `openfill.log` (+ `.1`, `.2`) | plug-in and supervisor log, rotated at 2 MB; prompts and file paths appear here, tokens do not |
| `comfyui.log` | ComfyUI's output |
| `server-state.json` | supervisor and ComfyUI pids, URL, status |
| `tasks/`, `tasks-status.json`, `tasks-worker.json` | download and install queue and progress |
| `models-cache.json` | model file names the server listed last |
| `hub-cache.json` | answers of the Hugging Face and Civitai searches, kept up to 24 hours (stale answers up to 7 days when offline); no token, no personal data; unreadable or odd records are ignored one by one |
| `hub-selected.json` | the live hub models you chose to download, re-validated by the live rules on every load; a malformed record is skipped, never fatal |
| `tmp/` | debug dumps (only with debug enabled): the crop, mask and results sent to and received from the backend, and the prompt |

Model files go to the configured ComfyUI folder (`<ComfyUI>/models/...`) and
custom node packs to `<ComfyUI>/custom_nodes/`. Uninstalling the plug-in
(`install.ps1 -Uninstall`, `sh install.sh --uninstall`) keeps the settings
folder unless `-RemoveConfig` / `--remove-config` is given, and never touches
models.

## What "Download ComfyUI" fetches

The notice that appears when ComfyUI is missing downloads exactly these things, all listed in `models/comfy_setup.json` and none chosen by the task that asks for them:

| What | From | How it is pinned and checked |
| --- | --- | --- |
| ComfyUI Windows portable build (NVIDIA, about 2 GB; the build for older NVIDIA cards or for AMD cards, about 1.6 to 1.9 GB, from the same release) | `https://github.com/comfyanonymous/ComfyUI/releases/download/<version>/...` (the URL must start with that prefix; the manifest is rejected otherwise) | Exact release version, size and SHA-256 (the digest GitHub publishes for the release asset) in the manifest. Size and hash are checked before the file is renamed from `.part`; a mismatch deletes it. |
| Unpacking | Windows' own `System32\tar.exe` (bsdtar), run with an argument list, never through a shell and never the `tar` on PATH | The archive is verified first. Unpacked into a scratch folder next to the target, then moved into place. No extra extractor is downloaded. |
| Inpaint nodes | `https://github.com/Acly/comfyui-inpaint-nodes/archive/<commit>.zip` (no git needed) | Full 40-character commit; the zip's comment must equal that commit; entries with absolute paths, `..`, drive letters or symbolic links are refused; a `.openfill-commit` marker records the pin. An existing git clone is reset to the pin with `git rev-parse HEAD` verified. |
| OpenCV | `opencv-python-headless` from PyPI, installed with ComfyUI's own embedded Python | Exact version (`==`) in the manifest. pip's own TLS and PyPI metadata apply; the plug-in does not hash it. |

The setup task runs in the supervisor, writes only below the install folder you chose (default `%LOCALAPPDATA%\OpenFill\ComfyUI`) and the plug-in's `config.json` (the `comfy_dir` setting, plus `--cpu` when you chose the processor-only route), and must be given an absolute folder. Before any of it, a pre-flight check reads the graphics card (`nvidia-smi`, then the adapter list from DXGI or the display driver's registry key, then the Windows adapter list through PowerShell), the free space on the target drive, and whether port 8188 answers; none of that leaves the PC. Existing ComfyUI installs are found by looking in fixed folders and in the config files of ComfyUI Desktop and Stability Matrix; nothing is sent anywhere.

## Network exposure

ComfyUI is started listening on the host of the configured URL, by default
`127.0.0.1`, so it is reachable only from this machine. ComfyUI has no
authentication: if you change the URL to a LAN address or `0.0.0.0`, anyone on
that network can queue work and read your generated images. The supervisor
logs a warning when the listen address is not a loopback address. The
plug-in's own HTTP and WebSocket clients connect to the configured URL, plus the
model hosts listed under "Live model search" and the download hosts.

## Telemetry

None. OpenFill collects no usage data, sends no analytics or crash reports,
and has no accounts. The only outbound requests are the ones listed under
"Live model search" and the model downloads you start yourself.

## Live model search

Every time the Models tab opens, the plug-in asks `huggingface.co` and
`civitai.com` for current models (read-only API calls, user agent
`gimp-openfill`, never your token) and caches the answers in
`hub-cache.json`. Responses are capped at 16 MB and a malformed answer only
drops that item. Set `hub_search` to false in `config.json` to turn the search
off, or `hub_civitai` to false to skip Civitai (no dialog option yet).

Hash policy for live models:

- A Civitai file is only offered with a published SHA-256.
- A Hugging Face file is only offered when the repository reports a commit; the
  URL is pinned to that commit, never to `main`. Its SHA-256 comes from the
  Hub's file listing.
- When a Hugging Face file has no published SHA-256, it is still listed, marked
  "unverified checksum", and the plug-in asks before downloading it. The bytes
  are then accepted as they arrive (trust on first use) and the computed SHA-256
  is recorded in `hub-selected.json`, so later downloads and repairs are verified.
- A download that is redirected from https to http is refused.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md). Please do not open a public issue for a
security problem.
