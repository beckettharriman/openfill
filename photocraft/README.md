# OpenFill for PhotoCraft

Generative Fill, Remove object, Generative Expand and Text to Image for [PhotoCraft](https://github.com/storytold/photocraft), running on your own graphics card through a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Nothing leaves your computer.

It's the same engine as [OpenFill for GIMP](https://github.com/beckettharriman/gimp-openfill): the same models, model manager, mask handling, seam correction and variations. Only the connection to the editor is different.

> **Prototype.** Tested end to end on Linux (Fedora 44, PhotoCraft 0.12.0, RTX 2070). Windows is written but not tested yet; macOS is not supported yet.

## Install (Linux)

1. Unzip `openfill-photocraft-<version>.zip` and run `sh install.sh` in the unzipped folder.
   - No PhotoCraft yet? The installer offers to download it for you (the official Linux build, SHA-256 checked, no admin rights).
   - It needs Python 3.10+ with Pillow and Tk. On Fedora: `sudo dnf install python3-pillow python3-tkinter`.
2. Open PhotoCraft the usual way: from the app menu, or by opening an image in the file manager. OpenFill comes with it.
3. The first time, the OpenFill bar asks for what's missing:
   - **ComfyUI**: if you use OpenFill for GIMP, the same ComfyUI is found by itself. Otherwise choose its folder, or follow the [install steps](https://github.com/beckettharriman/gimp-openfill#install).
   - **Models**: one click downloads LaMa (removing things, 0.2 GB) and DreamShaper 8 Inpainting (fill and expand, 2 GB). More models are under **Models and settings**.

To remove it: `openfill-photocraft-uninstall`. Your settings and models stay.

The installer gives PhotoCraft's menu entry to OpenFill (a copy in `~/.local/share/applications` that starts PhotoCraft through OpenFill). If you'd rather keep them apart, run `sh install.sh --separate-icon` and use "PhotoCraft with OpenFill" instead.

## Install (Windows, not tested yet)

Install PhotoCraft (the `.msi`), then double-click `Install OpenFill for PhotoCraft.cmd` and open **PhotoCraft with OpenFill** from the Start menu or the desktop. With ComfyUI's portable Python (no Tk), the OpenFill panel opens as a small Edge window instead of the bar.

## Using it

Select an area in PhotoCraft (Marquee, Lasso, Object Selection), then:

- type what you want in the **OpenFill bar** and press Enter, or
- pick an entry from **Filter > Plug-ins**: OpenFill Generative Fill, Remove Object (runs right away), Generative Expand, Text to Image, or OpenFill Panel to bring the bar back.

The bar stays on top of PhotoCraft:

| Tab | What it does |
|---|---|
| Fill | Fills the selection from your prompt, or from the surroundings with no prompt. Up to 4 variations. |
| Remove | Erases what you selected and continues the background. |
| Expand | Grows the canvas to +25% or an aspect ratio (1:1, 4:3, 3:2, 16:9, 9:16) and paints the new edges. |
| Create | Text to Image as a new image or a layer in this one (square, landscape or portrait). |

While it works you see a live preview. With several variations, click the thumbnails to compare them in PhotoCraft, then **Keep**, **Keep all** or **Discard**.

Every result is a new layer with a mask (variations go into a group), added in **one step**: one Undo takes it back. Your selection stays as it was, and the clipboard is never touched.

ComfyUI starts in the background when PhotoCraft opens, so the first Generate doesn't wait for it, and it stops when PhotoCraft closes.

## Good to know

- **Use OpenFill's way to open PhotoCraft** (the menu entry the installer set up). A PhotoCraft opened another way has no control channel; OpenFill notices and asks you to close it and open it again.
- **Images opened while it runs** (double-click in the file manager) open as tabs in the same PhotoCraft, as long as they are in your home folder.
- **8-bit round trip.** Pixels travel as 8-bit PNG files, because PhotoCraft's control channel doesn't offer direct pixel access yet ([storytold/photocraft#7779](https://github.com/storytold/photocraft/issues/7779)). On 16-bit documents the generated layer is 8-bit.
- **Wayland:** PhotoCraft answers OpenFill only while its window is drawing. If it's minimized or fully covered when a result is ready, the bar says so and OpenFill waits until you bring it back.
- **The Filter > Plug-ins entries** need a pixel layer to be active (PhotoCraft greys out filters on groups and text). The bar works with any layer.
- **Where things are kept:** `~/.config/openfill-photocraft` (settings, logs, models list, and the `exchange` folder PhotoCraft writes its temporary files to). Models stay in your ComfyUI folder, shared with OpenFill for GIMP.

## How it works

PhotoCraft has no plug-in system that can reach an AI backend yet, so OpenFill runs next to it:

- The launcher starts PhotoCraft with its **control channel** switched on (a local, password-protected connection for automation) and shows the OpenFill bar.
- The Filter menu entries are tiny WebAssembly filters that change nothing. Picking one leaves a history step named after it; OpenFill sees it, removes it, and opens the bar or starts the job.
- OpenFill reads the image and the selection through a merged duplicate (your document is not changed), runs ComfyUI, builds the result layers in a scratch document and copies them in with one command.

| Step | PhotoCraft commands |
|---|---|
| Read the image and selection | `image.duplicate` (merged), `select.saveSelection` into the duplicate, `channel.duplicate`, `app.save` |
| Build the result | `app.open` of a canvas-sized PNG per layer, `layer.copyToDocument`, `select.loadSelection`, `select.modify.expand` / `feather`, `layer.layerMask.revealSelection`, `layer.new.group` |
| Put it in the document | one `layer.copyToDocument` |
| Expand | `image.crop` past the canvas edges |
| Menu entries | `plugin.install`, then `document.inspect` and `history.deleteState` |

Tests: `python -m unittest photocraft/tests/test_companion.py` (no PhotoCraft needed). With PhotoCraft running: `photocraft/e2e/run_e2e.py` (the page's API) and `photocraft/e2e/run_bar_e2e.py` (the bar and the Filter menu).
