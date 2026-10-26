# Installing OpenFill

The short steps are in the [README](../README.md#install). This page has the rest: Linux and macOS, installing by hand, the install script options, uninstalling, and what to do when something goes wrong.

## What you need

- **A graphics card with 8 GB or more** for the SDXL and FLUX models: NVIDIA, or AMD Radeon RX 7000 or newer (new, and not tested on an AMD card yet; see [AMD graphics cards](#amd-graphics-cards)). LaMa (Remove) and the SD 1.5 models run on less. Without a card ComfyUI can use, it runs on the processor, which takes minutes per fill.
- **ComfyUI.** On Windows, OpenFill downloads it for you the first time (about 2 GB, about 7 GB unpacked). On Linux and macOS you set it up once (steps below). If you already have ComfyUI, point OpenFill at it.
- **Disk space** for the models: from 0.2 GB (LaMa) to about 13 GB (FLUX.1 Fill Q6).
- **For PhotoCraft:** PhotoCraft 0.12 to 0.16, Windows 10 or 11 or Linux, and Python 3.10 or newer with Tk and Pillow. The Python from python.org has Tk, and the Windows installer adds Pillow if it is missing. macOS is not supported yet.
- **For GIMP:** GIMP 3.2 or newer on Windows, Linux or macOS. OpenFill uses GIMP's own Python and needs no extra packages.

The steps below mention the **model manager**: that's where you get models and see what fits your graphics card. In PhotoCraft it's the gear icon in the OpenFill panel, Models section (the **Open model manager** button on the first run goes there too). In GIMP it's `Filters > OpenFill > Generative Fill...`, Models tab.

## OpenFill for PhotoCraft

### Windows

1. Install PhotoCraft (the `.msi` from its [releases page](https://github.com/storytold/photocraft/releases)).
2. Install Python from [python.org](https://www.python.org/downloads/windows/) if you don't have it (the default options are fine).
3. Unzip `openfill-photocraft-0.2.2.zip` and double-click **Install OpenFill for PhotoCraft.cmd**. It adds **PhotoCraft with OpenFill** to the Start menu and the desktop.
4. Open **PhotoCraft with OpenFill**. PhotoCraft opens with the OpenFill panel next to it.
5. The first time, the panel asks for what's missing:
   - **AI engine not found**: press **Download ComfyUI**. It downloads and unpacks in the background into `%LOCALAPPDATA%\OpenFill\ComfyUI`. With an AMD Radeon RX 7000 or newer it gets ComfyUI's version for AMD cards. If you already have ComfyUI, press **Choose folder** and pick the folder that has `main.py` in it (or the portable folder).
   - **No models installed**: press **Open model manager** and get LaMa (removing things, 0.2 GB) and DreamShaper 8 Inpainting (fill, expand and create, 2 GB).

If no Python with Tk is found, the installer uses ComfyUI's own Python and the panel opens as a small Edge window instead.

### Linux

1. On Fedora, install the panel's packages: `sudo dnf install python3-pillow python3-tkinter`.
2. Unzip `openfill-photocraft-0.2.2.zip` and run `sh install.sh` in the unzipped folder. No PhotoCraft yet? The installer offers to download the official Linux build (SHA-256 checked, no admin rights).
3. Open PhotoCraft the usual way: from the app menu, or by opening an image in the file manager. OpenFill comes with it.
4. The first time, the panel asks for what's missing. ComfyUI: choose its folder, or follow ComfyUI's [manual install](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux) first (AMD card: [AMD on Linux](#amd-on-linux)). Models: **Open model manager**.

The installer gives PhotoCraft's menu entry to OpenFill (a copy in `~/.local/share/applications` that starts PhotoCraft through OpenFill). If you'd rather keep them apart, run `sh install.sh --separate-icon` and use "PhotoCraft with OpenFill" instead.

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

On the M3, a 512 px fill with DreamShaper 8 takes 50 to 90 s once warm, Remove object with LaMa about 5 s, and the first request of a session another 20 to 70 s while ComfyUI loads. A selection larger than about 700 px is filled in tiles, which takes several minutes per tile pass on a Mac; fill large areas in a few smaller pieces. Intel Macs are untested.

GIMP's own Python can only be started by GIMP on macOS, so OpenFill runs its background helper (the supervisor that starts and stops ComfyUI and does the downloads) with ComfyUI's Python. OpenFill's windows show up in the Dock as "Python"; that is how GIMP runs Python plug-ins on macOS.

### Linux and Flatpak

Start GIMP once so the profile exists, then:

```sh
sh install.sh --comfy-dir ~/ComfyUI
```

`install.sh` finds the GIMP 3 profile on Linux and in the Flatpak (`~/.config/GIMP/3.2`). Point `--comfy-dir` at a ComfyUI checkout with a `venv` (it also finds one in `~/ComfyUI`). Tested on Fedora with the GIMP 3.2.6 Flatpak: Generative Fill starts ComfyUI from inside the sandbox and stops it when GIMP closes. The graphics card is read from the NVIDIA driver's own library, or for an AMD card from `/sys/class/drm`, so the Recommended for this PC list is right even inside the Flatpak, which has no `nvidia-smi`. A venv made with `uv venv` has no pip; OpenFill adds it with `ensurepip` when it needs to. The Download ComfyUI button is Windows-only.

## What Download ComfyUI does (Windows)

It checks your graphics card, free disk space and port 8188 first, then fetches the official Windows portable build (about 2 GB, about 7 GB unpacked) into `%LOCALAPPDATA%\OpenFill\ComfyUI`, checks it against a published SHA-256, unpacks it with Windows' own `tar.exe`, adds the inpaint nodes at a pinned commit and OpenCV, and remembers the folder. Git is not needed. OpenFill for PhotoCraft and OpenFill for GIMP use the same folder.

With an AMD Radeon RX 7000 or newer it fetches ComfyUI's version for AMD cards instead, from the same release and checked the same way (see [AMD graphics cards](#amd-graphics-cards)). For an older Radeon it points to ComfyUI's AMD instructions, after which you choose "I already have ComfyUI". Without a card it can use, it says so and offers the download only as "very slow" (the processor does the work).

## AMD graphics cards

New in OpenFill for PhotoCraft 0.2.2 and OpenFill for GIMP 2.0.0-beta.6. I don't have an AMD card (both my laptops are NVIDIA), so none of this has been tested on a real AMD card yet. If you try it, please tell me how it went on [Issues](https://github.com/beckettharriman/openfill/issues), good or bad.

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
