<div align="center">

# OpenFill

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) ![GIMP 3.2](https://img.shields.io/badge/GIMP-3.2-444.svg) ![Tested on Windows, Linux and macOS](https://img.shields.io/badge/tested%20on-Windows%20%7C%20Linux%20%7C%20macOS-444.svg) ![Python + ComfyUI](https://img.shields.io/badge/built%20with-Python%20%2B%20ComfyUI-orange.svg) ![Local](https://img.shields.io/badge/100%25-local-30d158.svg) ![Beta](https://img.shields.io/badge/status-beta-e8a33d.svg)

*Generative fill, expand and object removal for GIMP 3, running on your graphics card. You own the whole pipeline, nothing leaves your computer.*

<img src="docs/screenshots/openfill-demo.gif" width="720" alt="In GIMP, Generative Expand turns a 4:3 landscape photo of a car on a mountain road into a 3:4 portrait with no prompt, then the car is lassoed and Remove object erases it, leaving the empty road"><br>
**Remove objects, fill a selection from a prompt, and expand the canvas, all inside GIMP.**<br>

</div>


> [!IMPORTANT]
> **Beta.** This is an early release of OpenFill. It works end to end on Windows with GIMP 3.2.6 and an 8 GB laptop GPU, which is what I build and test on. It also runs end to end on Linux (GIMP 3.2.6 Flatpak on Fedora, RTX 2070 Max-Q), where the demo above was recorded, and on macOS (GIMP 3.2.6, Apple M3); the macOS steps are under Install. Most of the rough edges are in what the models draw, not in the plug-in; [Status](#status) lists them.

## Why I made this

I moved to GIMP 3 as my Photoshop replacement after paying Adobe an outrageously expensive cancellation fee. GIMP turned out to be a great alternative, but Adobe's AI image editing tools had become part of my workflow and I wasn't ready to give them up, so I made OpenFill. It's meant to feel familiar and easy to use, especially if you're coming from Photoshop. The selection is the mask, the result is a layer you can paint on or delete, and the AI backend (ComfyUI) starts hidden, stays warm while you work, and shuts down when GIMP closes. No terminal, no browser tab, no second app to babysit.

Here's what you get:

|                        |                                                                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| **Fill with a prompt** | Select, type "a red door with brass handles", Enter. The fill lands on a new layer with a mask that fades at the edges. |
| **Remove object**      | Select, switch to Remove object, Enter. No prompt. It erases the selection and continues the background.                |
| **Generative Expand**  | Drag the canvas edges outward or pick a ratio, and it paints the new border. The prompt is optional.                    |
| **Text to Image**      | A new layer or a new image from a prompt, sized to the selection if there is one.                                       |

Everything is under `Filters > OpenFill`. Results always go on new layers, in one undo step, so your existing layers are never painted over.

## Nothing leaves your computer

Generation runs in ComfyUI on your machine, listening on `127.0.0.1` only. A small supervisor starts it hidden, watches GIMP, and stops it after an idle timeout or when GIMP closes. `Filters > OpenFill > Stop AI Backend` frees the graphics card right away.

The network comes into it twice: when the Models tab searches Hugging Face and Civitai (no account, no token, and you can turn it off), and when you press Download (a model, or ComfyUI itself when none is installed). Every file is checked against its size and SHA-256 before it's used. The gated FLUX files need a free Hugging Face token; it stays in your GIMP profile and only ever goes to Hugging Face. No accounts, no telemetry, nothing that phones home: the only outbound requests are the ones above. Details in [docs/security.md](docs/security.md).

## The dialogs

| | |
|:---:|:---:|
| <a href="docs/screenshots/fill-dialog.png"><img src="docs/screenshots/fill-dialog.png" height="250" alt="Generative Fill"></a> | <a href="docs/screenshots/fill-dialog-remove.png"><img src="docs/screenshots/fill-dialog-remove.png" height="250" alt="Remove object"></a> |
| **Generative Fill.** Prompt, Recent prompts, what to avoid, the model and the number of variations are all on the first screen. Change amount and seed sit under Advanced. | **Remove object.** Same dialog, other tab. Nothing to type: it uses an installed removal model (LaMa or MAT) when there is one. |
| <a href="docs/screenshots/expand-dialog.png"><img src="docs/screenshots/expand-dialog.png" height="400" alt="Generative Expand"></a> | <a href="docs/screenshots/progress-window.png"><img src="docs/screenshots/progress-window.png" height="400" alt="Progress"></a> |
| **Generative Expand.** Drag any edge of the thumbnail outward, or pick a ratio (1:1, 4:5, 3:2, 16:9 and the rest) and where the image sits on a 3x3 grid. It shows the new size as you drag. | **Progress.** Floats over your image without stealing focus: the step, time left, a live preview of the picture forming, and Cancel. |
| <a href="docs/screenshots/settings-models.png"><img src="docs/screenshots/settings-models.png" height="290" alt="Models"></a> | <a href="docs/screenshots/variations-picker.png"><img src="docs/screenshots/tiles/variations-picker.png" height="290" alt="Variations"></a> |
| **Models.** The download manager. What you have installed first, then Recommended for this PC, then the rest, from Hugging Face and Civitai. | **Variations.** Ask for up to eight and click through them. The ones you don't keep stay hidden in a layer group in case you change your mind. |

## Models, and what fits your PC

OpenFill doesn't ship a model. On first run it opens the download manager, where you get and manage models without leaving GIMP. Its Recommended for this PC list only shows models that fit your graphics memory and free disk space, so you never find out after a 7 GB download that a model is too big.

| Job | Default model | Graphics memory | Time |
|---|---|---|---|
| Remove object | LaMa (MAT also available) | 2 GB | about 3 s |
| Fast fill | DreamShaper 8 Inpainting | 4 GB | about 9 s |
| Quality fill | RealVisXL V5 with the Fooocus inpaint patch | 8 GB | about 21 s |
| Generative Expand | SD 1.5 Inpainting, then DreamShaper 8 | 4 GB | |

Times are for a 512 px selection on an RTX 4070 Laptop (8 GB); the first request of a session adds about 35 s while the backend starts. FLUX.1 Fill (8 GB as a GGUF, 16 GB full size) and Qwen-Image-Edit (12 GB) are opt-in and much slower. Without an NVIDIA card, SD 1.5 runs on the CPU in minutes rather than seconds; AMD GPUs are untested; on an Apple M3 a warm fill takes 50 to 90 s.

The defaults come from a judged benchmark of 33 cases per model. Every download is checked against a published SHA-256, and the live Hugging Face and Civitai search never offers pickled `.ckpt` or `.bin` files, since those can run code when loaded. Details in [docs/models.md](docs/models.md) and [docs/performance.md](docs/performance.md).

Already have models? OpenFill also finds your own SD 1.5 inpainting and SDXL checkpoints in ComfyUI's `checkpoints` folder, and FLUX.1 Fill files in `unet` or `diffusion_models`, and lists them in the model menu as "your file", with no download. It reads only `.safetensors` and `.gguf` files, and only their headers, to tell the model family apart.

## Install

1. **Install [GIMP 3.2](https://www.gimp.org/downloads/).**
2. **Add the plug-in.** Download `openfill-<version>.zip` from the [Releases page](https://github.com/beckettharriman/gimp-openfill/releases), unzip it, and drag the `gimp-openfill` folder into GIMP's plug-ins folder (`Edit > Preferences > Folders > Plug-ins`; on Windows that's `%APPDATA%\GIMP\3.2\plug-ins`).
3. **Restart GIMP and run `Filters > OpenFill > Generative Fill...`.** If ComfyUI isn't found, **Download ComfyUI** sets it up for you (Windows with an NVIDIA card, about 2 GB), or **I already have ComfyUI** lets you pick an existing install.
4. **Get the starting models** when the model manager opens: LaMa (object removal, 0.2 GB) and DreamShaper 8 Inpainting (fill and expand, 2 GB). The first request starts the backend in about a minute; after that it's seconds.

<details>
<summary><b>What Download ComfyUI does</b></summary>

It checks your graphics card, free disk space and port 8188 first, then fetches the official Windows portable build (about 2 GB, about 7 GB unpacked) into `%LOCALAPPDATA%\OpenFill\ComfyUI`, checks it against a published SHA-256, unpacks it with Windows' own `tar.exe`, adds the inpaint nodes at a pinned commit and OpenCV, and remembers the folder. Git is not needed.

Without an NVIDIA card the notice says so and offers the download only as "very slow" (the processor does the work). For an AMD card it points to ComfyUI's AMD instructions, after which you choose "I already have ComfyUI". The same notice appears, instead of an error, whenever you start Fill, Remove or Expand and ComfyUI can't be found.

</details>

<details>
<summary><b>If something goes wrong</b></summary>

Every error message ends with what to do next, and the same advice shows in the progress window. For a bug report, turn on Verbose log in Settings, do it again, and attach the log file. Never attach `config.json`, it may hold your token. Settings are created the first time you change one; until then the defaults are used.

</details>

<details>
<summary><b>Manual install on Windows</b></summary>

You need GIMP 3.2 or newer and ComfyUI. OpenFill uses GIMP's own Python and needs no extra packages.

**Installer script.** The zip also holds `Install OpenFill.cmd` (a thin wrapper around `install.ps1`, which you can read). It does the same copy for you, looks for an existing ComfyUI (portable build, ComfyUI Desktop, Stability Matrix, Pinokio), and gives you `-Uninstall`, `-Link` and headless model downloads. From a clone, close GIMP and run:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**ComfyUI by hand.** Download the portable build from the [ComfyUI releases](https://github.com/Comfy-Org/ComfyUI/releases), extract it (Windows 10 and 11 can open `.7z` with `tar -xf`), and point OpenFill at the folder that holds `python_embeded` in `Filters > OpenFill > Generative Fill Settings`. Any other ComfyUI checkout with a `venv` works too. Add the [comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) at the commit pinned in `models/catalog.json` (the Models tab does this for you, together with OpenCV, which fills and Generative Expand need to continue the picture into the new area). If OpenCV is still missing when you run a fill, OpenFill says so and installs it in the background. To add it yourself:

```powershell
python_embeded\python.exe -s -m pip install opencv-python-headless
```

To build the release zip yourself: `python tools/make_release_zip.py`.

</details>

<details>
<summary><b>macOS</b></summary>

Tested end to end on a MacBook Air M3 (16 GB, macOS 14.7) with GIMP 3.2.6: fill, remove, expand, text to image, variations, the Models tab downloads, and the backend starting on the Apple GPU and stopping when GIMP closes. The Download ComfyUI button is Windows-only, so ComfyUI is set up once in Terminal.

1. **Install GIMP 3.2** from [gimp.org](https://www.gimp.org/downloads/) (or `brew install --cask gimp`), open it once and quit it.
2. **Install ComfyUI** in `~/ComfyUI` with a `venv`. It needs Python 3.10 or newer; the `/usr/bin/python3` that comes with macOS is too old, so use one from [python.org](https://www.python.org/downloads/macos/) or `brew install python@3.13`:

   ```sh
   git clone https://github.com/comfyanonymous/ComfyUI.git ~/ComfyUI
   cd ~/ComfyUI
   python3.13 -m venv venv
   venv/bin/pip install torch torchvision torchaudio
   venv/bin/pip install -r requirements.txt opencv-python-headless
   ```

   OpenCV (`opencv-python-headless`) matters more here than it sounds: without it Generative Expand paints the new border from a flat colour and the result rarely matches the photo. If you leave it out, OpenFill installs it into this venv the first time a fill needs it, and tells you.
3. **Install the plug-in.** From the unzipped release or a clone: `sh install.sh`. It finds `~/ComfyUI` by itself (otherwise add `--comfy-dir <folder>`).
4. **Restart GIMP** and run `Filters > OpenFill > Generative Fill...`; download LaMa and DreamShaper 8 Inpainting when the model manager opens.

On a MacBook Air M3 with 16 GB, a 512 px fill with DreamShaper 8 takes 50 to 90 s once warm (25 steps at 2 to 3 s each; up to about 2.5 minutes after a long session on this fanless laptop), Remove object with LaMa about 5 s, and the first request of a session another 20 to 70 s while ComfyUI loads. A selection larger than about 700 px is filled in tiles, which costs several minutes per tile pass on a Mac (a 1040 x 800 px fill on a 4000 x 3000 photo took about 14 minutes); fill large areas in a few smaller pieces. Intel Macs are untested.

GIMP's own Python can only be started by GIMP on macOS, so OpenFill runs its background helper (the supervisor that starts and stops ComfyUI and does the downloads) with ComfyUI's Python. OpenFill's windows show up in the Dock as "Python"; that is how GIMP runs Python plug-ins on macOS.

</details>

<details>
<summary><b>Linux and Flatpak</b></summary>

`install.sh` finds the GIMP 3 profile on Linux and Flatpak. Start GIMP once first so the profile exists, then:

```sh
sh install.sh --comfy-dir ~/ComfyUI
```

Tested on Fedora with the GIMP 3.2.6 Flatpak (its profile is in `~/.config/GIMP/3.2`): Generative Fill starts ComfyUI from inside the sandbox and stops it when GIMP closes. Point `--comfy-dir` at a ComfyUI checkout with a `venv` (`install.sh` also finds one in `~/ComfyUI`). The graphics card is read from the NVIDIA driver's own library, so the Recommended for this PC list is right on the first run even inside the Flatpak, which has no `nvidia-smi`. A venv made with `uv venv` has no pip; OpenFill adds it with `ensurepip` when it needs to install the inpaint nodes' requirements or OpenCV. The Download ComfyUI button is Windows-only.

</details>

<details>
<summary><b>Install script options</b></summary>

| `install.ps1` | `install.sh` | Does |
|---|---|---|
| `-Link` | `--link` | Links the plug-in to your checkout instead of copying it, for development. |
| `-ProfileDir <dir>` | `--profile-dir <dir>` | Installs into a specific GIMP profile. `GIMP3_DIRECTORY` works too. |
| `-ComfyDir <dir>` | `--comfy-dir <dir>` | Uses this ComfyUI instead of looking for one. |
| `-DetectComfy` | | Lists the ComfyUI installs found on this PC. |
| `-Uninstall` | `--uninstall` | Removes the plug-in. Keeps your settings and never touches models. |
| `-RemoveConfig` | `--remove-config` | With uninstall, also deletes the settings folder (it holds the Hugging Face token, if any). |
| `-ListModels`, `-Model <id>` | | Lists or downloads a model without opening GIMP, with resume and checksum. |

The plug-in installs as `gimp-openfill/` and keeps settings in `<GIMP profile>/openfill/config.json`.

</details>

## Status

Works end to end on Windows, on Linux (GIMP 3.2.6, including the Flatpak) and on macOS (GIMP 3.2.6, Apple M3): fill, remove, expand, text to image, the model manager, variations, and the backend starting and stopping itself. On Windows, 25 failure scenarios (no backend, corrupt settings, port in use, ComfyUI or GIMP killed mid-job, no network, cancel at every stage) pass in a real GIMP, and the unit tests run on any Python 3.10 or newer.

Where the models still fall short:

- **SD 1.5 often ignores the prompt** on flat areas and small selections. Pick an SDXL model in the dialog.
- **Remove object struggles with very large holes.** LaMa smears and MAT invents shapes; both are fine on small objects.
- **Generative Expand gets weaker beyond 25% per side.** Expand in rounds; OpenFill also splits large expansions itself.
- Fine textures like fabric come back softer in large fills.

Not tested yet: FLUX and Qwen end to end, Intel Macs, and the AUTOMATIC1111 backend. Next up: ControlNet and reference images, and LoRA selection. Changes are in [CHANGELOG.md](CHANGELOG.md); [CONTRIBUTING.md](CONTRIBUTING.md) covers the tests.

## Where it came from

OpenFill started as a GIMP 3 port of Torben Giesselmann's [Stable Boy](https://github.com/thndrbrrr/gimp-stable-boy); thanks to him for the idea and the head start. The backend is [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Acly's [comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) do the inpainting, and his [krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion) was the reference for how to build the graphs and manage the server. The model authors and their licenses are listed in [docs/models.md](docs/models.md).

Open, as in open source and on your own machine. Fill, as in the thing everyone else rents you by the month.

## License

[Apache-2.0](LICENSE) © 2026 Beckett Harriman.
