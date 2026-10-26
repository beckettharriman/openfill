# Installing OpenFill

The short steps are in the [README](../README.md#install). This page has the rest: Linux and macOS, installing by hand, the install script options, uninstalling, and what to do when something goes wrong.

## What you need

- **A graphics card with 8 GB or more** for the SDXL and FLUX models: NVIDIA, or AMD Radeon RX 7000 or newer (see [AMD graphics cards](#amd-graphics-cards)). LaMa (Remove) and the SD 1.5 models run on less. Without a card ComfyUI can use, it runs on the processor, which takes minutes per fill.
- **ComfyUI.** On Windows, OpenFill downloads it for you the first time (about 2 GB, about 7 GB unpacked). On Linux and macOS you set it up once (steps below). If you already have ComfyUI, point OpenFill at it.
- **Disk space** for the models: from 0.2 GB (LaMa) to about 13 GB (FLUX.1 Fill Q6).
- **For PhotoCraft:** PhotoCraft 0.12 to 0.16, Windows 10 or 11 or Linux, and Python 3.10 or newer with Pillow. ComfyUI's existing Python works; Tk is optional. The Python from python.org has Tk, and the Windows installer adds Pillow if it is missing. PhotoCraft 0.2.3 adds Apple Silicon support, tested on an M3. See [PhotoCraft on macOS](#photocraft-on-macos).
- **For GIMP:** GIMP 3.2 or newer on Windows, Linux or macOS. OpenFill uses GIMP's own Python and needs no extra packages.

The steps below mention the **model manager**: that's where you get models and see what fits your graphics card. In PhotoCraft it's the gear icon in the OpenFill panel, Models section (the **Open model manager** button on the first run goes there too). In GIMP it's `Filters > OpenFill > Generative Fill...`, Models tab.

## OpenFill for PhotoCraft

### Windows

1. Install PhotoCraft (the `.msi` from its [releases page](https://github.com/storytold/photocraft/releases)).
2. If you already have ComfyUI, you can use its Python without installing another one; see [using ComfyUI's Python](#use-comfyuis-python). Otherwise, install Python from [python.org](https://www.python.org/downloads/windows/).
3. Unzip `openfill-photocraft-0.2.4.zip` and double-click **Install OpenFill for PhotoCraft.cmd**. It adds **PhotoCraft with OpenFill** to the Start menu and the desktop.
4. Open **PhotoCraft with OpenFill**. PhotoCraft opens with the OpenFill panel next to it.
5. The first time, the panel asks for what's missing:
   - **AI engine not found**: press **Download ComfyUI**. It downloads and unpacks in the background into `%LOCALAPPDATA%\OpenFill\ComfyUI`. With an AMD Radeon RX 7000 or newer it gets ComfyUI's version for AMD cards. If you already have ComfyUI, press **Choose folder** and pick the folder that has `main.py` in it (or the portable folder).
   - **No models installed**: press **Open model manager** and get LaMa (removing things, 0.2 GB) and DreamShaper 8 Inpainting (fill, expand and create, 2 GB).

If the selected Python has no Tk, the OpenFill panel opens in a tab in your default browser. PhotoCraft stays in its own window. No browser is installed or chosen for you. See [using ComfyUI's Python](#use-comfyuis-python).

### Linux

1. Use your existing ComfyUI Python as described below. If you prefer a separate Python and native panel, on Fedora run `sudo dnf install python3-pillow python3-tkinter`.
2. Unzip `openfill-photocraft-0.2.4.zip` and run `sh install.sh` in the unzipped folder. No PhotoCraft yet? The installer offers to download the official Linux build (SHA-256 checked, no admin rights).
3. Open PhotoCraft the usual way: from the app menu, or by opening an image in the file manager. OpenFill comes with it.
4. The first time, the panel asks for what's missing. ComfyUI: choose its folder, or follow ComfyUI's [manual install](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux) first (AMD card: [AMD on Linux](#amd-on-linux)). Models: **Open model manager**.

The installer gives PhotoCraft's menu entry to OpenFill (a copy in `~/.local/share/applications` that starts PhotoCraft through OpenFill). If you'd rather keep them apart, run `sh install.sh --separate-icon` and use "PhotoCraft with OpenFill" instead.


## Use ComfyUI's Python

The default-browser fallback and explicit interpreter priority require OpenFill for PhotoCraft 0.2.4 or newer.

You do not need a second Python installation. OpenFill can run in ComfyUI's Python environment on Windows, macOS and Linux. It needs Python 3.10 or newer and Pillow, which ComfyUI normally already has. Selecting this interpreter does not change ComfyUI's packages.

Open a terminal in the unzipped OpenFill folder. Set the path to the Python you already use for ComfyUI, then run the installer.

Windows portable ComfyUI, in **Command Prompt**:

```bat
set "OPENFILL_PYTHON=C:\ComfyUI_windows_portable\python_embeded\python.exe"
"Install OpenFill for PhotoCraft.cmd"
```

macOS or Linux, for example:

```sh
OPENFILL_PYTHON="$HOME/ComfyUI/.venv/bin/python" sh install.sh
```

Change the example path to match your installation. A virtual environment may be named `venv` instead of `.venv`; Windows virtual environments use `Scripts\python.exe`. Paths with spaces work. The launcher remembers the chosen Python. If you move or remove that environment, rerun the installer with its new path.

With Tk available, OpenFill shows its separate panel. Without Tk, it opens the same controls in a normal tab in your default browser on all three platforms. The page runs locally. Keep PhotoCraft open while using it. To try this without installing launchers on macOS or Linux, set the same variable and run `sh openfill-photocraft.sh`.

### After updating PhotoCraft

The launcher looks for PhotoCraft each time it starts, rather than storing a version number. An update in the usual installed location should be found automatically. OpenFill keeps its ComfyUI folder setting and models separate from the PhotoCraft installation. Explicit `--photocraft` or `PHOTOCRAFT_BIN` paths take priority. Portable copies, moved apps or old symlinks may still point to an older version, so keep one intended copy in the standard location or update that explicit path. A future PhotoCraft API change may still need an OpenFill update; automatic discovery is not a compatibility guarantee.

## OpenFill for GIMP 3

### Windows

1. Install [GIMP 3.2](https://www.gimp.org/downloads/). Open it once, then close it.
2. Unzip `openfill-2.0.0-beta.6.zip` and double-click `Install OpenFill.cmd`, or drag the `gimp-openfill` folder into `%APPDATA%\GIMP\3.2\plug-ins`.
3. Restart GIMP and run `Filters > OpenFill > Generative Fill...`. If ComfyUI isn't found, **Download ComfyUI** sets it up for you, or **I already have ComfyUI** lets you pick an existing install.
4. When the model manager opens, get LaMa (0.2 GB) and DreamShaper 8 Inpainting (2 GB).

### Manual install on Windows

`Install OpenFill.cmd` is a thin wrapper around `install.ps1`, which you can read. It copies the plug-in, looks for an existing ComfyUI (portable build, ComfyUI Desktop, Stability Matrix, Pinokio), and has `-Uninstall`, `-Link` and headless model downloads. From a clone, close GIMP and run:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

ComfyUI by hand: download the portable build from the [ComfyUI releases](https://github.com/Comfy-Org/ComfyUI/releases), extract it (Windows 10 and 11 can open `.7z` with `tar -xf`), and point OpenFill at the folder that holds `python_embeded` in `Filters > OpenFill > Generative Fill Settings`. Any other ComfyUI checkout with a `venv` works too. The Models tab adds the [comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) at the commit pinned in `models/catalog.json`, together with OpenCV, which fills and Generative Expand need to continue the picture into the new area. If OpenCV is still missing when you run a fill, OpenFill says so and installs it in the background. To add it yourself:

```powershell
python_embeded\python.exe -s -m pip install opencv-python-headless
```

To make the release zips yourself: `python tools/make_release_zip.py` (GIMP) and `python tools/make_photocraft_zip.py` (PhotoCraft).

### macOS

Tested end to end on a MacBook Air M3 (16 GB, macOS 14.7) with GIMP 3.2.6. The Download ComfyUI button is Windows-only, so ComfyUI is set up once in Terminal.

1. Install GIMP 3.2 from [gimp.org](https://www.gimp.org/downloads/) (or `brew install --cask gimp`), open it once and quit it.
2. Install ComfyUI in `~/ComfyUI` with a `venv`. It needs Python 3.10 or newer; the `/usr/bin/python3` that comes with macOS is too old, so use one from [python.org](https://www.python.org/downloads/macos/) or `brew install python@3.13`:

   ```sh
   git clone https://github.com/comfyanonymous/ComfyUI.git ~/ComfyUI
   cd ~/ComfyUI
   python3.13 -m venv venv
   venv/bin/pip install torch torchvision torchaudio
   venv/bin/pip install -r requirements.txt opencv-python-headless
   ```

   Without OpenCV (`opencv-python-headless`) Generative Expand paints the new border from a flat colour. If you leave it out, OpenFill installs it into this venv the first time a fill needs it, and tells you.
3. Run `sh install.sh` in the unzipped release or a clone. It finds `~/ComfyUI` by itself (otherwise add `--comfy-dir <folder>`).
4. Restart GIMP and run `Filters > OpenFill > Generative Fill...`; get LaMa and DreamShaper 8 Inpainting when the model manager opens.

On the M3, a 512 px fill with DreamShaper 8 takes 50 to 90 s once warm, Remove object with LaMa about 5 s, and the first request of a session another 20 to 70 s while ComfyUI loads. A selection larger than about 700 px is filled in tiles, which takes several minutes per tile pass on a Mac; fill large areas in a few smaller pieces. Intel Macs are untested. A fresh [2.0.0-beta.6 check](evidence/macos-gimp-2026-10-26/) on October 26 used a smaller 512 × 384 scene at 25 steps: Fill 38.6 s including startup, LaMa Remove 1.9 s, Expand right by 64 pixels 25.5 s, and Create as a layer 24.6 s. These runs use different settings from the earlier timings.

GIMP's own Python can only be started by GIMP on macOS, so OpenFill runs its background helper (the supervisor that starts and stops ComfyUI and does the downloads) with ComfyUI's Python. OpenFill's windows show up in the Dock as "Python"; that is how GIMP runs Python plug-ins on macOS.

### PhotoCraft on macOS

Tested on a MacBook Air M3, 16 GB, macOS 14.7, with PhotoCraft 0.16.0. Download [openfill-photocraft-0.2.4.zip](https://github.com/beckettharriman/openfill/releases/download/photocraft-v0.2.4/openfill-photocraft-0.2.4.zip) and unzip it.

1. Get the macOS DMG from [PhotoCraft releases](https://github.com/storytold/photocraft/releases) and copy PhotoCraft to `/Applications` or `~/Applications`.
2. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/macos/), which includes Tk. Run `python3 -m pip install Pillow`. You can instead use your existing ComfyUI Python with Pillow; Tk is optional. See [using ComfyUI's Python](#use-comfyuis-python).
3. Set up ComfyUI as described [above](#macos), or use your existing checkout and models. No second model download is needed when GIMP already uses that ComfyUI.
4. Open Terminal in the unzipped `openfill-photocraft-0.2.4` folder and run `sh install.sh`.
5. Open `~/Applications/PhotoCraft with OpenFill.app`. In Settings > Engine, choose `~/ComfyUI` if it is not found. Settings > Models lists your existing models; LaMa and DreamShaper 8 cover all four tools.
6. Open an image, select an area, and try Fill or Remove. Expand adds canvas space; Create makes an image from a prompt.

The panel floats independently on macOS. Its automatic following of PhotoCraft when moving or minimizing is Windows-only. Install does not replace PhotoCraft or change file associations. To remove OpenFill, close its PhotoCraft session and open `~/Applications/Uninstall OpenFill for PhotoCraft.app`, or run `sh install.sh --uninstall` from the source/archive folder. Settings, PhotoCraft and ComfyUI models stay in place.

At eight steps with a 512 × 384 image, the M3 checks took 20.1 s for Fill, 4.0 s for LaMa Remove, 14.1 s for a 32-pixel Expand on every edge, and 12.1 s for Create. These are small functional checks; eight-step Expand showed edge artifacts. Larger images and more steps take longer. Undo, cancellation, Finder launch, native settings/model lists, engine start/stop, and uninstall/reinstall were also checked. See [the evidence and remaining checks](evidence/macos-photocraft-2026-10-26/). Other Apple chips and Intel Macs have not been tested.

### Linux and Flatpak

Start GIMP once so the profile exists, then:

```sh
sh install.sh --comfy-dir ~/ComfyUI
```

`install.sh` finds the GIMP 3 profile on Linux and in the Flatpak (`~/.config/GIMP/3.2`). Point `--comfy-dir` at a ComfyUI checkout with a `venv` (it also finds one in `~/ComfyUI`). Tested on Fedora with the GIMP 3.2.6 Flatpak: Generative Fill starts ComfyUI from inside the sandbox and stops it when GIMP closes. The graphics card is read from the NVIDIA driver's own library, or for an AMD card from `/sys/class/drm`, so the Recommended for this PC list is right even inside the Flatpak, which has no `nvidia-smi`. A venv made with `uv venv` has no pip; OpenFill adds it with `ensurepip` when it needs to. The Download ComfyUI button is Windows-only.

## Use a ComfyUI you already have

For a normal portable or manual installation:

1. Find the folder you use to start ComfyUI. A portable installation contains both `python_embeded` and `ComfyUI`. A manual installation contains `main.py` and a `venv` or `.venv` folder.
2. In PhotoCraft, open OpenFill and press **Choose folder**. After setup, use the gear icon, **Engine**, **Browse**, then save. In GIMP, use **I already have ComfyUI** on the setup screen or change the ComfyUI folder in Generative Fill Settings.
3. Select that folder. Keep its Python environment and models in place.
4. Open **Models**. Supported files appear there. OpenFill recognises SD 1.5 inpainting, SDXL and FLUX.1 Fill files; not every model that ComfyUI runs is supported.
5. Pick an installed model and try a small selection. A model may need extra files or nodes, which Models will show.

Choosing a folder saves its location. Later, generation and model installation can add OpenFill's node pack, other required nodes or Python packages to that engine. For an engine managed by another app, use the guidance below first. Models stored only through `extra_model_paths.yaml` are not all discovered by OpenFill's local scanner yet.

### Krita AI Diffusion (Acly)

Do not choose Krita's managed server folder as OpenFill's engine. Its Python and model layout differs, and OpenFill may install nodes or Python packages that affect Krita. Use **Download ComfyUI** or a separate manual/portable ComfyUI for OpenFill. Sharing Krita's checkpoints without copying is still under testing and is not included in this checkout.

### ComfyUI Desktop

**Choose folder does not support the older Electron Desktop layout.** Its data folder has `.venv` and models, while `main.py` lives in the application resources. Selecting either half does not give OpenFill the right Python, model paths and launch options together. Earlier automatic discovery of Desktop's data folder was not sufficient to establish support.

A newer Desktop installation that has `main.py` and `.venv` together inside its actual ComfyUI checkout passes the normal folder check. That is layout detection, not an end-to-end Desktop compatibility test, and Desktop-managed shared model paths are not automatically imported. For now use a separate portable or manual ComfyUI for OpenFill.

## What Download ComfyUI does (Windows)

It checks your graphics card, free disk space and port 8188 first, then fetches the official Windows portable build (about 2 GB, about 7 GB unpacked) into `%LOCALAPPDATA%\OpenFill\ComfyUI`, checks it against a published SHA-256, unpacks it with Windows' own `tar.exe`, adds the inpaint nodes at a pinned commit and OpenCV, and remembers the folder. Git is not needed. OpenFill for PhotoCraft and OpenFill for GIMP use the same folder.

With an AMD Radeon RX 7000 or newer it fetches ComfyUI's version for AMD cards instead, from the same release and checked the same way (see [AMD graphics cards](#amd-graphics-cards)). For an older Radeon it points to ComfyUI's AMD instructions, after which you choose "I already have ComfyUI". Without a card it can use, it says so and offers the download only as "very slow" (the processor does the work).

The download includes ComfyUI and its own Python with the packages needed to run it. You do not need to install the ComfyUI Desktop app separately. It does not include image models. Open the model manager afterwards and choose what to download: LaMa is about 0.2 GB and DreamShaper 8 Inpainting about 2 GB. Larger models and their extra files are listed in [Models](models.md#models-and-their-licenses).

### Long Windows paths

Keep ComfyUI near the drive root, for example `C:\AI\ComfyUI`. Deeply nested folders can cause a missing-file error during startup, including a Transformers file with `audio_spectrogram_transformer` in its name. This can happen before the graphics card does any work.

OpenFill checks the projected dependency path before downloading or starting ComfyUI. If it is too long, use a shorter location and choose it in OpenFill. For an existing portable installation, close its engine first and move the whole portable folder, including Python and models, to the shorter location. For an installation managed by another app, keep its files in place; a short Windows directory junction can provide another path to the same folder. Do not move only the models or change graphics drivers to address this error.

## AMD graphics cards

New in OpenFill for PhotoCraft 0.2.2 and OpenFill for GIMP 2.0.0-beta.6. A user [tested PhotoCraft 0.2.2 on an RX 7800 XT](https://github.com/beckettharriman/openfill/issues/3): Create, Fill, Remove and Expand worked with DreamShaper and LaMa after shortening the engine path. Other AMD cards and models still need testing. If you try it, please tell me how it went on [Issues](https://github.com/beckettharriman/openfill/issues), good or bad.

ComfyUI runs on AMD cards through ROCm, AMD's version of CUDA. LaMa, MAT, the SD 1.5 and SDXL models and the Fooocus inpaint patch are plain PyTorch, so they should work the same as on NVIDIA. The GGUF versions of FLUX.1 Fill and Qwen Image Edit go through ComfyUI-GGUF, which has an open report of noise-only images on an RX 7900 with ROCm ([issue 300](https://github.com/city96/ComfyUI-GGUF/issues/300)). On an AMD card the model manager says so next to them and leaves them out of Recommended for this PC. OpenFill reads the card's name and graphics memory without AMD's tools (from DXGI or the driver's registry key on Windows, `/sys/class/drm` on Linux), so Recommended for this PC fits AMD cards too.

ComfyUI sets what it needs for AMD cards itself (attention, MIOpen), so OpenFill adds no options. If pictures come out black, add `--fp32-vae` in Extra ComfyUI options (GIMP: Generative Fill Settings > Connection and troubleshooting; PhotoCraft: gear icon, Engine).

### AMD on Windows

ComfyUI makes a Windows version for AMD cards, `ComfyUI_windows_portable_amd.7z` (PyTorch 2.9.1 with ROCm 7.2.1). It runs on the Radeon RX 7000 and RX 9000 series, Radeon PRO W7000 and AI PRO R9000, and the Ryzen AI 300 and Ryzen AI Max chips (Radeon 880M, 890M, 8050S, 8060S). With one of those, **Download ComfyUI** gets it instead of the NVIDIA version: about 1.6 GB (about 6 GB unpacked), from the same ComfyUI release, checked against its SHA-256. Keep the AMD graphics driver up to date.

Older Radeons (RX 6000 and earlier, and the Radeon 780M and 760M) are not covered by it. For those, ComfyUI's [instructions for AMD on Windows](https://github.com/comfyanonymous/ComfyUI#amd-gpus-windows-rocm-100) use AMD's newer PyTorch packages, which also run RDNA 2 (RX 6000) cards. They need Windows 11 and 64-bit Python 3.13. In October 2026 they come down to:

```powershell
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
py -3.13 -m venv venv
venv\Scripts\pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ "torch[device-all]==2.13.0+rocm10.0.0" "torchvision[device-all]==0.28.0+rocm10.0.0" "torchaudio==2.11.0.2+rocm10.0.0"
venv\Scripts\pip install -r requirements.txt opencv-python-headless
```

Then press **Choose folder** (PhotoCraft) or **I already have ComfyUI** (GIMP) and pick that `ComfyUI` folder. DirectML and ZLUDA are not supported: ComfyUI itself warns that DirectML "barely works", and ZLUDA is a separate, unofficial fork.

### AMD on Linux

Install ComfyUI with PyTorch for ROCm, as [ComfyUI's instructions](https://github.com/comfyanonymous/ComfyUI#amd-gpus-linux) say:

```sh
git clone https://github.com/comfyanonymous/ComfyUI.git ~/ComfyUI
cd ~/ComfyUI
python3 -m venv venv
venv/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.2
venv/bin/pip install -r requirements.txt opencv-python-headless
```

Your user needs to be in the `render` and `video` groups (`sudo usermod -aG render,video $USER`, then log out and in). Then choose `~/ComfyUI` in the panel (PhotoCraft) or run `sh install.sh --comfy-dir ~/ComfyUI` (GIMP).

ROCm officially supports the RX 7700 and up and the RX 9000 series on Linux. The RX 6700 and 6600 (and other RDNA 2 cards except the 6800 and 6900) need `HSA_OVERRIDE_GFX_VERSION=10.3.0`, and the Radeon 780M and 760M `11.0.0`, as ComfyUI's README says. OpenFill sets that by itself when it starts ComfyUI for one of those cards (unless you set it already) and writes it in its log.

## Install script options (GIMP)

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

## Uninstall

- **PhotoCraft on Windows:** run `Install OpenFill for PhotoCraft.cmd --uninstall` from a command prompt in the unzipped folder.
- **PhotoCraft on Linux:** `openfill-photocraft-uninstall`.
- **GIMP on Windows:** `powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall` in the unzipped folder (add `-RemoveConfig` to delete the settings too).
- **GIMP on macOS and Linux:** `sh install.sh --uninstall` (add `--remove-config` to delete the settings too).

PhotoCraft and GIMP stay installed. Your models stay in your ComfyUI folder, and ComfyUI stays where it is.

## If something goes wrong

Every error message ends with what to do next, and in GIMP the same advice shows in the progress window. For a bug report, turn on Verbose log in Settings, do it again, and attach the log file to an [issue](https://github.com/beckettharriman/openfill/issues). Never attach `config.json`, it may hold your Hugging Face token.

- **PhotoCraft was opened another way:** open it with "PhotoCraft with OpenFill" (or the menu entry the installer set up on Linux). A PhotoCraft opened another way has no control channel; OpenFill notices and asks you to close it and open it again.
- **Where things are kept:** `%APPDATA%\OpenFill for PhotoCraft` on Windows and `~/.config/openfill-photocraft` on Linux for the PhotoCraft version, `<GIMP profile>/openfill` for the GIMP plug-in. Settings in the GIMP plug-in are created the first time you change one; until then the defaults are used.

### One-click uninstall (PhotoCraft 0.2.3)

Close OpenFill, open **Uninstall OpenFill for PhotoCraft**, then confirm. On Windows it is in the Start menu; on Linux it is in the app menu; on macOS it is in `~/Applications`. PhotoCraft, settings and models stay in place. The command-line uninstall steps above still work for older installations. The Mac command-line uninstall and reinstall passed on the M3; the confirmation dialog still needs a hands-on check.
