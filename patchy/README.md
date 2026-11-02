# OpenFill for Patchy

Generative Fill, Remove object, Generative Expand and Text to Image for [Patchy](https://github.com/SethRobinson/Patchy), running on your own graphics card through a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI). Local generation stays on your computer.

It uses the same engine and the same panel as [OpenFill for PhotoCraft](../photocraft/README.md): the same models, model manager, mask handling, seam correction and variations. Only the connection to the editor is different.

> **Version 0.1.0** is a prototype. It needs a Patchy build with **Plugins > Companions** and the `resizeCanvas` options, which is not in a Patchy release yet. It was tested end to end on Windows 11 against a Patchy 1.24 development build, with a stand-in generator in place of ComfyUI (no graphics card or models); a run with a real ComfyUI and real models is still to do. See [the test results](../docs/evidence/patchy-2026-11-02/).

## What you need

- **Patchy** with Plugins > Companions, on Windows, macOS or Linux.
- **Python 3.10 or newer with Pillow.** Tk is optional: with it you get the OpenFill panel; without it, the same controls open in your default browser. ComfyUI's own Python works too.
- **ComfyUI and models**, as for [OpenFill for PhotoCraft](../photocraft/README.md#what-you-need). If OpenFill for PhotoCraft or for GIMP already has a ComfyUI folder set, it is used.

## Install

From the repository (or an unzipped copy of it), in the `patchy` folder:

```sh
python -m openfill_patchy.install
```

This adds **OpenFill** to Patchy's **Plugins > Companions** menu for your user account. It writes one file, `openfill.json`, into Patchy's companions folder (`%APPDATA%\RTsoft\Patchy\companions` on Windows, `~/Library/Application Support/RTsoft/Patchy/companions` on macOS, `~/.local/share/RTsoft/Patchy/companions` on Linux). The entry runs OpenFill with the Python you ran the installer with; pass `--python PATH` to choose another one. Patchy reads the folder each time the menu opens, so there is nothing to restart.

To remove it: `python -m openfill_patchy.install --uninstall`. Settings and models stay.

## Using it

Pick **Plugins > Companions > OpenFill** in Patchy. The first time, Patchy starts OpenFill; after that the same menu entry brings the panel back. Select an area with any of Patchy's selection tools, type what you want in the panel and press Enter. The modes (Fill, Remove, Expand, Create) are the ones described in [OpenFill for PhotoCraft](../photocraft/README.md#using-it).

Every result is a new layer with a layer mask (variations go into a group), and it is **one step** in Patchy's history: one Undo takes a fill or a whole expand back. **Keep** leaves it one step, and **Discard** takes it back completely, as long as you have not changed the document since. After an edit of your own, Keep and Discard delete layers instead, so your edit is never undone.

The panel follows your selection as you change it. On Windows it stays with Patchy's window: above it, and hidden when Patchy is minimized. Elsewhere it floats. OpenFill stops when Patchy closes.

## Models

The same as [OpenFill for PhotoCraft](../photocraft/README.md#models), from the same model manager (the gear in the panel). Models stay in your ComfyUI folder, shared with OpenFill for PhotoCraft and for GIMP.

## Good to know

- **The mask comes from the selection you had when you pressed Generate**, so you can select something else while it works.
- **Expand and the Background layer.** Expand grows the canvas like Image > Canvas Size: the image and the selection move, and a Background layer grows to cover the new canvas, filled with the image's mean colour. The result covers the new area; the fill shows only where the result leaves a gap.
- **Patchy busy.** While you drag, or while a dialog is open, Patchy asks OpenFill to wait. OpenFill tries again on its own, and the panel says Patchy is not answering only when it has waited for more than a few seconds.
- **8-bit round trip.** Pixels cross at 8 bits per channel; on a 16 or 32-bit document Patchy converts the result to the document's depth.
- **Where things are kept:** `%APPDATA%\OpenFill for Patchy` on Windows, `~/Library/Application Support/OpenFill for Patchy` on macOS and `~/.config/openfill-patchy` on Linux (settings, logs, and the `exchange` folder for the PNG files OpenFill and Patchy pass to each other).

## Known limits

The resolution limits are those of [OpenFill for PhotoCraft](../photocraft/README.md#known-limits): the engine is the same.

Not tested yet: a real ComfyUI with real models, macOS and Linux, and the panel next to a visible Patchy window (picking the menu entry, the Windows tether).

## How it works

Patchy has a companion-app interface instead of an in-process plug-in: OpenFill runs Patchy's connector, `patchy-mcp --attach`, as a child process and talks to the running Patchy over it (JSON-RPC on standard input and output, the same channel AI assistants use). Patchy sets `PATCHY_MCP` and `PATCHY_MCP_ENDPOINT` when it starts a companion, so OpenFill finds the right connector and the right Patchy window.

| Step | Patchy calls |
|---|---|
| Read the image and selection | one script run: `doc.getCompositePixels`, `doc.selection.getMask`, `patchy.io.writeImage`. Not a history step |
| Put the result in the document | one script run: `addLayer`, `setPixels`, `setLayerMask`, `addGroup` and `moveLayers` for variations. One history step |
| Expand | `resizeCanvas` with an offset and a fill, in that same run |
| Keep and Discard | Keep: one run that undoes OpenFill's step (`doc.undo`) and adds only the chosen layer. Discard: the `undo` tool. Both only while the history is as OpenFill left it; after an edit of your own, a run that deletes layers |
| Selection and menu | Patchy's events (`selectionChanged`, `companionCommand`), so nothing is polled |

The result is kept in OpenFill while the seam correction, grain matching and ranking run, and reaches Patchy only at the end. Every edit carries Patchy's state token from just before it, so an edit never lands on a document that changed under it, and Keep or Discard never undo a step you made. A request Patchy reports as lost is never sent again.

Tests: `python -m unittest patchy/tests/test_companion.py` (no Patchy needed; a fake connector, and a stand-in generator for a whole Generative Fill). With a Patchy build: `python patchy/e2e/run_e2e.py --patchy PATH/patchy.exe` starts its own Patchy offscreen (its own settings and connector endpoint, so a Patchy you have open is never touched), runs Fill, variations with Keep, Discard, Expand and Create with the stand-in generator from `patchy/tests/stub_backend.py`, and writes `report.json` and the document's image after each job.

## Credits and license

OpenFill for Patchy is Apache-2.0 (see `LICENSE`). It runs on the same ComfyUI, node packs and models as [OpenFill for PhotoCraft](../photocraft/README.md#credits-and-license), under their own licenses. Patchy is a separate project by Seth Robinson.
