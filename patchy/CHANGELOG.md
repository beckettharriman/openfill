# Changelog

All notable changes to OpenFill for Patchy are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-20

Patchy uses the same panel and engine as OpenFill for PhotoCraft, so it gets the same fixes:

- The Model menu fits the screen, scrolls, has a search field with 10 or more models, and models can be hidden from it ([#14](https://github.com/beckettharriman/openfill/issues/14)).
- If another ComfyUI or another program used port 8188, OpenFill used that one or its own ComfyUI failed to start ([#12](https://github.com/beckettharriman/openfill/issues/12)). It now starts its own on the next free port and says so in the status line.
- On Linux, the AMD setting for Radeon cards is no longer used when ComfyUI runs on an NVIDIA card in the same computer ([#12](https://github.com/beckettharriman/openfill/issues/12)).
- When the engine takes longer than 90 seconds to start, the status line says so, and a model folder on an unplugged or sleeping drive is named when it doesn't answer ([#12](https://github.com/beckettharriman/openfill/issues/12)).
- The Qwen-Image-Edit 2511 download no longer fails its check: every model download comes from a fixed version of the file ([#13](https://github.com/beckettharriman/openfill/issues/13)).
- A large Fill with SDXL or SD 1.5 and more than one variation no longer fails with "Image and mask batch size does not match" ([#16](https://github.com/beckettharriman/openfill/issues/16)). The same for RORem and the FLUX eraser.
- The panel no longer fails to open with some Python 3.14 and Tk 8.6 setups ("bad screen distance "1.5"") ([#16](https://github.com/beckettharriman/openfill/issues/16)).
- On Windows with display scaling above 100 %, the panel and OpenFill's messages are sharp instead of blurry, at the same size as before ([#17](https://github.com/beckettharriman/openfill/issues/17)).
- Google Gemini as a third optional cloud service, by Kaya Furkan İpekci (kayafurkanipekci) ([#19](https://github.com/beckettharriman/openfill/pull/19)). Pick it under Engine > Cloud service. Cloud service also has a Time limit in seconds (0 = automatic).
- Remove Background (Cutout tab) keeps the image's own transparency, by Pedro (PedroArthurr) ([#15](https://github.com/beckettharriman/openfill/pull/15)). See-through parts such as glass or a soft shadow stay see-through and keep their own colour, and the cutout is never more opaque than the original. A fully transparent image now says there is nothing to cut out instead of adding an empty layer.
- Upscale tab, from the same panel as PhotoCraft: a bigger copy of the whole image, 2x or 4x, as a new document. Needs Real-ESRGAN from Models.
- FLUX.2 [klein] 4B for Fill and Expand, and your own ComfyUI workflow (Generation > Advanced) as one more Fill model, as in PhotoCraft.
- Remove object with LaMa: big removals on a pattern, such as bricks, tiles or planks, now continue the pattern. A selection longer than 256 px came back as a flat grey patch with a fine grid, because LaMa only learned to fill holes about that big. It's now cleared at the size where its longer side is 256 px, scaled back up and pasted into the selection only, so everything around it stays sharp. Smaller selections still run at full size.

## [0.1.0] - 2026-11-08

- Remove Background (Cutout tab): cuts the subject out into a new layer with BiRefNet, by Pedro (PedroArthurr) ([#11](https://github.com/beckettharriman/openfill/pull/11)), from the same panel as PhotoCraft. Needs ComfyUI 0.39 or newer and BiRefNet from Models.
- Add OpenFill for Patchy 0.1.0 (prototype): the OpenFill panel as a Patchy companion app, started from Plugins > Companions, with each result one step in Patchy's history.
- Grow the canvas for Expand with Patchy's `resizeCanvas` offset, so the Background layer covers the new canvas, filled with the image's mean colour.
- Say Patchy is not answering only after it has stayed busy for a few seconds, so dragging a selection's handles no longer flashes the warning.
- Quit OpenFill from Patchy's Plugins > Companions menu; it stops the way it does when Patchy closes.
- Give the keyboard back to Patchy after Generate, Keep, Keep all or Discard on Windows, so Patchy's shortcuts work without a click.
- Check Patchy's companion protocol version at start (OpenFill 0.1.0 speaks version 1) and say whether Patchy or OpenFill needs updating; the menu entry records the same range.
- Support Patchy's Flatpak: the installer also writes the menu entry into the Flatpak's companions folder, and OpenFill started by hand uses the Flatpak's own connector.
- Check for Pillow in the Python the menu entry runs, not the installer's own, and suggest ComfyUI's Python where pip cannot add packages (Homebrew, Ubuntu's system Python).
- Say how to start OpenFill when Patchy's connector cannot be found.
- Support Patchy's AppImage: started from its Companions menu, OpenFill runs the AppImage's connector (`patchy-mcp` as the AppImage's argument), and `--connector` accepts the AppImage file.
- Start the OpenFill page without looking up the computer's name, which took 35 s on a Mac with slow name lookups.
