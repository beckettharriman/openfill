# PhotoCraft macOS test checklist

Updated 2026-10-26. The M3 session is complete; see [results and remaining checks](macos-photocraft-2026-10-26/). The earlier [GIMP M3 evidence](macos-2026-10-16/) does not establish PhotoCraft compatibility.

## Preparation

Use the unreleased checkout or a test archive containing these changes. The public 0.2.2 archive does not include the Mac launcher.

1. Install PhotoCraft in `/Applications` or `~/Applications` and open it once normally.
2. Install Python 3.10 or newer with Tk from python.org. Install Pillow with that interpreter: `python3 -m pip install Pillow`.
3. From the checkout's `photocraft` folder, run `sh install.sh`. In an unpacked companion archive, run the same command from its top folder. The installer chooses a Python with Tk and Pillow.
4. Open `~/Applications/PhotoCraft with OpenFill.app`. It starts PhotoCraft and the companion. The launcher preserves filenames passed to it, including spaces.
5. For a local engine, follow [macOS ComfyUI setup](../install.md#macos), then choose that folder in OpenFill. Reuse the GIMP test engine only after confirming its location and keeping its models/settings intact.

The launcher lives in `~/Applications`; its code lives in `~/Library/Application Support/OpenFill for PhotoCraft/app`. It does not replace PhotoCraft's application or change file associations.

## Hardware checks

Record macOS, Python, PhotoCraft, ComfyUI and model versions, then:

- Open a disposable image, show the panel, switch tools, resize the window, minimize and restore it. Check focus, Dock behavior and native menus.
- Fill a small selection with DreamShaper 8. Confirm a new layer, one Undo, cancellation and useful error text.
- Remove an object with LaMa; Expand each edge; Create one image.
- Test a filename containing spaces, restart the launcher, and reopen settings.
- Record cold and warm timings and memory pressure. Do not claim the GIMP timings as PhotoCraft measurements.
- Open `~/Applications/Uninstall OpenFill for PhotoCraft.app`, cancel once, then open it and confirm removal. The command-line fallback is `PYTHONPATH=. python3 -m openfill_photocraft.install --uninstall` from the source/archive folder. Confirm the launcher/code are removed, PhotoCraft still opens, and settings/models remain.

Local tests cover launcher paths with spaces, arguments, refusal to overwrite an unrelated app, and uninstall preserving settings. They cannot verify Cocoa, accessibility, PhotoCraft process control or Apple GPU execution. The linked hardware report records what was verified in the authorized M3 session.
