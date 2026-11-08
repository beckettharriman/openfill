# Changelog

All notable changes to OpenFill for Patchy are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
