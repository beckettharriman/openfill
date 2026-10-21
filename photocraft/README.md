# OpenFill for PhotoCraft

Generative Fill, Remove object, Generative Expand and Text to Image for [PhotoCraft](https://github.com/storytold/photocraft), running on your own graphics card through a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Nothing leaves your computer.

It's the same engine as [OpenFill for GIMP](https://github.com/beckettharriman/gimp-openfill): the same models, model manager, mask handling, seam correction and variations. Only the connection to the editor is different.

> **Prototype.** Tested end to end on Linux (Fedora, PhotoCraft 0.11.1, RTX 2070). Windows and macOS are not tested yet.

## How it works

PhotoCraft has no plug-in system that can reach an AI backend yet, so OpenFill runs next to it:

- The launcher starts PhotoCraft with its **control channel** switched on (a local, password-protected connection that PhotoCraft offers for automation), then opens the OpenFill page in your browser.
- You select an area in PhotoCraft as usual, then type a prompt or press Remove on the OpenFill page.
- OpenFill reads the selection and the image from PhotoCraft, runs ComfyUI, and puts the result back as a new layer with a mask, exactly where it belongs. Several variations go into a group; click through them on the page and keep the one you like.

## Install (Linux)

1. Install [PhotoCraft](https://github.com/storytold/photocraft/releases) (the `.rpm`, `.deb`, AppImage or tarball).
2. Install ComfyUI in `~/ComfyUI` with a `venv` ([steps in the OpenFill README](https://github.com/beckettharriman/gimp-openfill#install), under Linux).
3. Unzip `openfill-photocraft-<version>.zip` anywhere and run `./openfill-photocraft.sh`.
   - Close PhotoCraft first: the launcher starts it with the control channel.
   - If PhotoCraft isn't found, pass its path: `./openfill-photocraft.sh --photocraft /path/to/photocraft`.
4. On the page, open **Models** and download LaMa (object removal, 0.2 GB) and DreamShaper 8 Inpainting (fill and expand, 2 GB).

The launcher needs Python 3.10 or newer with Pillow. ComfyUI's own venv has both, and the launcher tries it first.

## Good to know

- **Use the launcher to start PhotoCraft.** A PhotoCraft you opened yourself has no control channel, so OpenFill can't reach it.
- **8-bit round trip.** Pixels travel between OpenFill and PhotoCraft as 8-bit PNG files, because PhotoCraft's control channel doesn't offer direct pixel access yet ([storytold/photocraft#7779](https://github.com/storytold/photocraft/issues/7779)). On 16-bit documents the generated layer is 8-bit.
- **Each step is its own undo step** in PhotoCraft's history, so undoing one result can take several steps.
- **Where things are kept:** `~/.config/openfill-photocraft` (settings, logs, models list, and the `exchange` folder PhotoCraft may read and write). Models stay in your ComfyUI folder, shared with OpenFill for GIMP.

## Under the hood

| Step | PhotoCraft command over the control channel |
|---|---|
| Read the selection | `document.inspect` (bounds), `select.saveSelection`, `channel.duplicate`, save as PNG |
| Read the image | `image.duplicate` (merged), save as PNG |
| Put a result back | a canvas-sized PNG is opened, copied and pasted in place (pixel-exact), then `select.loadSelection`, `select.modify.expand` / `feather` and `layer.layerMask.revealSelection` |
| Expand | `image.crop` past the canvas edges |
| Variations | `layer.new.group`, `layer.moveTo`, `layer.setProps` |

Tests: `python -m unittest photocraft/tests/test_companion.py` (no PhotoCraft needed), and `photocraft/e2e/run_e2e.py` against a running companion and PhotoCraft.
