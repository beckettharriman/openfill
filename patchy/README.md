# OpenFill for Patchy

Generative Fill, Remove object, Generative Expand and Text to Image for [Patchy](https://github.com/SethRobinson/Patchy), with the same engine, models and panel as [OpenFill for PhotoCraft](../photocraft/README.md): a local [ComfyUI](https://github.com/comfyanonymous/ComfyUI) on your own graphics card. Local generation stays on your computer.

> **Prototype.** This needs a Patchy build with **Plugins > Companions**, which is not in a Patchy release yet. It has been checked end to end against such a build with a stand-in generator (no GPU); a run with a real ComfyUI and real models is still to do. See [What has been tested](#what-has-been-tested).

## What you need

- **Patchy** with Plugins > Companions, on Windows, macOS or Linux.
- **Python 3.10 or newer with Pillow.** Tk is optional: with it you get the OpenFill panel; without it, the same controls open in your default browser. ComfyUI's own Python works too.
- **ComfyUI and models**, as for OpenFill for PhotoCraft. If OpenFill for PhotoCraft or for GIMP already has a ComfyUI folder set, it is used.

## Install

From the repository (or an unzipped copy of it), in the `patchy` folder:

```sh
python -m openfill_patchy.install
```

This adds **OpenFill** to Patchy's **Plugins > Companions** menu for your user account. It writes one file, `openfill.json`, into Patchy's companions folder (`%APPDATA%\RTsoft\Patchy\companions` on Windows, `~/Library/Application Support/RTsoft/Patchy/companions` on macOS, `~/.local/share/RTsoft/Patchy/companions` on Linux). The entry runs OpenFill with the Python you ran the installer with; pass `--python PATH` to choose another one. Patchy reads the folder each time the menu opens, so there is nothing to restart.

To remove it: `python -m openfill_patchy.install --uninstall`. Settings and models stay.

## Using it

Pick **Plugins > Companions > OpenFill** in Patchy. The first time, Patchy starts OpenFill; after that the same menu entry brings the panel back. Select an area with any of Patchy's selection tools, type what you want in the panel and press Enter. The modes are the ones described in [OpenFill for PhotoCraft](../photocraft/README.md#using-it).

- Every result is a new layer with a layer mask (variations go into a group), and it is **one step** in Patchy's history: one Undo takes a fill or a whole expand back. **Keep** leaves it one step, and **Discard** takes it back completely, as long as you have not changed the document since. After an edit of your own, Keep and Discard delete layers instead, so your edit is never undone.
- The mask comes from the selection you had when you pressed Generate, so you can select something else while it works.
- The panel follows your selection as you change it; Patchy tells OpenFill, so nothing is polled.
- On Windows the panel stays with Patchy's window: above it, hidden when Patchy is minimized. Elsewhere it floats.
- OpenFill stops when Patchy closes.

## How it works

Patchy has a companion-app interface instead of an in-process plug-in: OpenFill runs Patchy's connector, `patchy-mcp --attach`, as a child process and talks to the running Patchy over it (JSON-RPC on standard input and output, the same channel AI assistants use). Patchy sets `PATCHY_MCP` and `PATCHY_MCP_ENDPOINT` when it starts a companion, so OpenFill finds the right connector and the right Patchy window.

- **Reading:** one script run writes the visible image (`doc.getCompositePixels`) and the selection with its soft edges (`doc.selection.getMask`) to PNG files in OpenFill's exchange folder. Reading is not a history step.
- **Writing:** the result is kept in OpenFill while the seam correction, grain matching and ranking run, then one script run adds the layers (`addLayer`, `setPixels`), their masks (`setLayerMask`) and the variations' group. An expand's canvas change goes into that same run. Patchy makes every script run one undo step.
- **Events:** OpenFill asks for Patchy's events when it connects. Patchy then tells it when the selection or the active document changes, and when you pick OpenFill in Plugins > Companions.
- **Safety:** every edit carries Patchy's state token from just before it, so an edit never lands on a document that changed under it, and Keep or Discard never undo a step you made. A request Patchy reports as lost is never sent again.

Pixels cross at 8 bits per channel; on a 16 or 32-bit document Patchy converts the result to the document's depth.

Settings, logs and the exchange folder are in `%APPDATA%\OpenFill for Patchy` on Windows, `~/Library/Application Support/OpenFill for Patchy` on macOS and `~/.config/openfill-patchy` on Linux.

## What has been tested

- `python -m unittest patchy/tests/test_patchy.py`: the connector client, the bridge, Keep and Discard, the event watcher and the installer, against a fake connector, plus OpenFill's own Generative Fill running through the bridge with a stand-in generator.
- `python patchy/e2e/run_patchy_e2e.py --patchy PATH/patchy.exe`: a real Patchy (offscreen, with its own settings and connector endpoint) on Windows 11, with the stand-in generator from `patchy/tests/stub_backend.py` in place of ComfyUI. It checks that a fill adds one masked layer that one Undo removes, that three variations go into a group and Keep leaves one step, that Discard leaves none, that an expand (two rounds) and an expand to the left of a locked Background layer are one step each, that Text to Image opens a new document, and that the selection events arrive.

Not tested yet: a real ComfyUI with real models, macOS and Linux, and the panel itself next to a visible Patchy window (picking the menu entry, the Windows tether).
