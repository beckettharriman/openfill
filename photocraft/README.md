# OpenFill for PhotoCraft

Generative Fill, Remove object, Generative Expand and Text to Image for [PhotoCraft](https://github.com/storytold/photocraft), running on your own graphics card through a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Nothing leaves your computer.

It uses the same engine as [OpenFill for GIMP](https://github.com/beckettharriman/openfill#install): the same models, model manager, mask handling, seam correction and variations. Only the connection to the editor is different.

<img src="../docs/screenshots/photocraft-demo.gif" width="720" alt="OpenFill in PhotoCraft: a fill from a prompt, an object removed and the canvas expanded">

> **Version 0.2.1** (the first release, 0.2.0, with two fixes). It works with PhotoCraft 0.12 to 0.16. I tested it end to end on Windows 11 with PhotoCraft 0.12.0 and an RTX 4070 Laptop GPU (8 GB), and on Linux (Fedora 44, RTX 2070 Max-Q) with PhotoCraft 0.16.0, with a shorter check on 0.12.0, 0.13.0, 0.14.0 and 0.15.1. macOS is not supported yet.

## What you need

- **PhotoCraft 0.12 to 0.16** (the Windows `.msi` or the Linux build from its [releases page](https://github.com/storytold/photocraft/releases)). The newest, 0.16.0, is fine.
- **Windows 10 or 11, or Linux.**
- **An NVIDIA graphics card**, 8 GB or more for the SDXL and FLUX models. LaMa (Remove) and the SD 1.5 models are fine on less. Without an NVIDIA card it runs on the processor, which is very slow.
- **ComfyUI.** On Windows the OpenFill panel downloads it for you the first time (a 1.9 GB download, 6.7 GB unpacked). If you use OpenFill for GIMP, the same ComfyUI is found by itself.
- **Python 3.10 or newer** with Tk and Pillow, for the OpenFill panel. On Windows, the Python from python.org has Tk, and the installer adds Pillow if it is missing. On Fedora: `sudo dnf install python3-pillow python3-tkinter`.
- **Disk space** for the models: from 0.2 GB (LaMa) to about 13 GB (FLUX.1 Fill Q6).

## Install on Windows

1. Install PhotoCraft (the `.msi`).
2. Install Python from [python.org](https://www.python.org/downloads/windows/) if you don't have it (the default options are fine).
3. Unzip `openfill-photocraft-0.2.1.zip` and double-click **Install OpenFill for PhotoCraft.cmd**. It adds **PhotoCraft with OpenFill** to the Start menu and the desktop.
4. Open **PhotoCraft with OpenFill**. PhotoCraft opens with the OpenFill panel next to it.
5. The first time, the panel asks for what's missing:
   - **AI engine not found**: press **Download ComfyUI**. It downloads and unpacks in the background (a few minutes) into `%LOCALAPPDATA%\OpenFill\ComfyUI`. If you already have ComfyUI, press **Choose folder** instead and pick the folder that has `main.py` in it (or the portable folder).
   - **No models installed**: press **Open model manager**. LaMa (removing things, 0.2 GB) and DreamShaper 8 Inpainting (fill, expand and create, 2 GB) cover every tool.

If no Python with Tk is found, the installer uses ComfyUI's own Python and the panel opens as a small Edge window instead.

To remove it, run `Install OpenFill for PhotoCraft.cmd --uninstall` from a command prompt in the unzipped folder. PhotoCraft stays installed, and your settings and models stay where they are.

## Install on Linux

1. Unzip `openfill-photocraft-0.2.1.zip` and run `sh install.sh` in the unzipped folder.
   - No PhotoCraft yet? The installer offers to download it for you (the official Linux build, SHA-256 checked, no admin rights).
2. Open PhotoCraft the usual way: from the app menu, or by opening an image in the file manager. OpenFill comes with it.
3. The first time, the panel asks for what's missing. ComfyUI: choose its folder, or follow ComfyUI's [manual install](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux) first. Models: **Open model manager**.

The installer gives PhotoCraft's menu entry to OpenFill (a copy in `~/.local/share/applications` that starts PhotoCraft through OpenFill). If you'd rather keep them apart, run `sh install.sh --separate-icon` and use "PhotoCraft with OpenFill" instead.

To remove it: `openfill-photocraft-uninstall`. Your settings and models stay.

## Using it

Select an area in PhotoCraft (Marquee, Lasso, Object Selection), then type what you want in the OpenFill panel and press Enter. You can also pick **Filter > Plug-ins > OpenFill** in PhotoCraft to bring the panel back.

| Mode | What it does |
|---|---|
| Fill | Fills the selection from your prompt, or from the surroundings with no prompt. Up to 5 variations. |
| Remove | Erases what you selected and continues the background. LaMa does it at the image's own resolution. The Model menu offers every installed model that can remove. |
| Expand | Grows the canvas: drag the frame's edges, or pick a ratio (Original adds 25 % on each side; 1:1, 5:4, 4:3, 3:2 and 16:9, landscape or portrait). It paints the new edges. |
| Create | Text to Image as a new image (square, landscape or portrait), or as a layer in the open image. With a selection, the layer fills the selection. |

While it works you see a live preview. With several variations, click the thumbnails to compare them in PhotoCraft, then **Keep**, **Keep all** or **Discard**.

Every result is a new layer with a mask (variations go into a group), and it is **one step** in PhotoCraft's history: one Undo takes a fill or a whole expand back. Keep leaves it one step, and Discard takes it back completely. Your selection stays as it was, and the clipboard is never touched.

The panel stays with PhotoCraft: it hides when PhotoCraft is minimized and comes back with it. The arrow at its top right folds it into a small strip, and a click on the strip opens it again. The gear opens **Models and settings**: install, uninstall and stop downloads under Models, ComfyUI under Engine, and the generation settings under Generation.

ComfyUI starts in the background when PhotoCraft opens, so the first Generate doesn't wait for it, and it stops when PhotoCraft closes.

## Models

The model manager shows what fits your graphics card. Each model has its own license; the model manager links to it.

| Model | Size | For | License |
|---|---|---|---|
| LaMa | 0.2 GB | Remove (default) | Apache-2.0 |
| MAT Places512 | 0.1 GB | Remove | **CC BY-NC 4.0, non-commercial** |
| RORem (SDXL eraser) | 6.5 GB | Remove | Apache-2.0 |
| FLUX.1 Fill object removal LoRA | 0.3 GB, on FLUX.1 Fill | Remove | MIT for the LoRA, but it runs on FLUX.1 Fill: **non-commercial** |
| DreamShaper 8 Inpainting | 2.0 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| SD 1.5 Inpainting, Realistic Vision V6 Inpainting | 4.0 / 2.0 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| RealVisXL V5, Juggernaut XL v9, SDXL base (with the Fooocus inpaint patch, 1.2 GB) | 6.5 GB each | Fill, Expand, Create | CreativeML OpenRAIL++-M / OpenRAIL-M |
| FLUX.1 Fill dev (GGUF Q4, GGUF Q6, full) | 10 to 27 GB | Fill, Expand | **FLUX.1 [dev] Non-Commercial License** |
| Qwen Image Edit 2511 (GGUF Q4) | 21 GB | Fill, Expand | Apache-2.0 |

Your own SD 1.5 inpainting, SDXL and FLUX.1 Fill files in ComfyUI's model folders show up too, marked "your file".

## Good to know

- **Use OpenFill's way to open PhotoCraft** ("PhotoCraft with OpenFill", or the menu entry the installer set up on Linux). A PhotoCraft opened another way has no control channel; OpenFill notices and asks you to close it and open it again.
- **Images opened while it runs** (double-click in the file manager) open as tabs in the same PhotoCraft, as long as they are in your home folder.
- **8-bit round trip.** Pixels travel as 8-bit PNG files, because PhotoCraft's control channel doesn't offer direct pixel access yet ([storytold/photocraft#7779](https://github.com/storytold/photocraft/issues/7779)). On 16-bit documents the generated layer is 8-bit.
- **FLUX on 8 GB is slow.** A FLUX.1 Fill fill took about 3 minutes on the RTX 4070 Laptop GPU, and the FLUX removal LoRA about 4. RealVisXL took 25 to 45 seconds, LaMa about 5.
- **Linux on Wayland:** PhotoCraft answers OpenFill only while its window is drawing. If it's minimized or fully covered when a result is ready, the panel says so and OpenFill waits until you bring it back. On Windows a minimized PhotoCraft keeps working.
- **The Filter > Plug-ins entry** needs a pixel layer to be active (PhotoCraft greys out filters on groups and text). The panel works with any layer.
- **Where things are kept:** `%APPDATA%\OpenFill for PhotoCraft` on Windows, `~/.config/openfill-photocraft` on Linux (settings, logs, and the `exchange` folder PhotoCraft writes its temporary files to). Models stay in your ComfyUI folder, shared with OpenFill for GIMP.

## Known limits

These are about resolution on large images. Remove is not affected: LaMa works at the image's own resolution.

- **Fill** with SD 1.5 or SDXL works at no less than 60 % (SD 1.5) or 75 % (SDXL) of full resolution, in overlapping windows when the selection is large. With **FLUX** or Qwen there are no windows yet: a large selection is drawn at about 1024 px on its long side and scaled up, so a 2000 px selection comes back at about half resolution.
- **Expand** draws the new area in one pass at about 1024 px with every model and scales it up: on a 3000 px canvas the new edges are drawn at about a third of full resolution and look soft. Expand in rounds on big images.
- **Create** makes images of up to 1024 px on the long side with SDXL (768 px with SD 1.5), in the shape you pick. As a layer in a larger selection it is scaled up to fit.
- **Variations** of a Remove with LaMa or MAT are all the same, so Remove makes one.

## How it works

PhotoCraft has no plug-in system that can reach an AI backend yet, so OpenFill runs next to it:

- The launcher starts PhotoCraft with its **control channel** switched on (a local, password-protected connection for automation) and shows the OpenFill panel.
- The Filter menu entry is a tiny WebAssembly filter that changes nothing. Picking it leaves a history step named after it; OpenFill sees it, removes it, and brings the panel up.
- OpenFill reads the image and the selection through a merged duplicate (your document is not changed), runs ComfyUI, builds the result layers in a scratch document and copies them in with one command.

| Step | PhotoCraft commands |
|---|---|
| Read the image and selection | `image.duplicate` (merged), `select.saveSelection` into the duplicate, `channel.duplicate`, `app.save` |
| Build the result | `app.open` of a canvas-sized PNG per layer, `layer.copyToDocument`, `select.loadSelection`, `select.modify.expand` / `feather`, `layer.layerMask.revealSelection`, `layer.new.group` |
| Put it in the document | one `layer.copyToDocument` |
| Expand | `image.crop` past the canvas edges, then the steps made into one with `history.newSnapshot` and `history.selectSnapshot` |
| Keep and Discard | `history.deleteState` (only while the history is as OpenFill left it) |
| Menu entry | `plugin.install`, then `document.inspect` and `history.deleteState` |

Tests: `python -m unittest photocraft/tests/test_companion.py` (no PhotoCraft needed). With PhotoCraft running: `photocraft/e2e/run_e2e.py` (the panel's API) and `photocraft/e2e/run_bar_e2e.py` (the panel and the Filter menu). `photocraft/e2e/smoke_install.py` installs, starts and uninstalls it against a real PhotoCraft (the Windows CI job runs it).

## Credits and license

OpenFill for PhotoCraft is Apache-2.0 (see `LICENSE`). It runs on [ComfyUI](https://github.com/comfyanonymous/ComfyUI), [comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) by Acly (LaMa, MAT and the Fooocus inpaint patch) and [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) by city96 (the FLUX and Qwen GGUF files), which OpenFill adds to ComfyUI when a model needs them. The models belong to their authors under the licenses above. PhotoCraft is by its own authors; OpenFill is not affiliated with it.
