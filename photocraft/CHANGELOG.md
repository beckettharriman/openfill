# Changelog

All notable changes to OpenFill for PhotoCraft are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-25

First public release.

### Added

- Generative Fill, Remove object, Generative Expand and Text to Image for
  PhotoCraft 0.12.0, running on a local ComfyUI with OpenFill's engine (the
  same models, model manager, seam correction and variations as OpenFill for
  GIMP).
- The OpenFill panel next to PhotoCraft: Fill, Remove, Expand and Create, a
  live preview, variations with thumbnails, and Models and settings behind the
  gear. It folds into a small strip, and on Windows it stays with PhotoCraft's
  window (it hides when PhotoCraft is minimized and comes back with it).
- Filter > Plug-ins > OpenFill in PhotoCraft brings the panel up.
- Remove object at the image's own resolution with LaMa, through OpenFill's
  own ComfyUI node. MAT, RORem, the FLUX.1 Fill object removal LoRA and every
  fill model can be picked for Remove too.
- Windows installer (`Install OpenFill for PhotoCraft.cmd`): "PhotoCraft with
  OpenFill" in the Start menu and on the desktop. It adds Pillow to a
  python.org Python that has Tk but not Pillow.
- Windows first run: **Download ComfyUI** in the panel downloads the same
  checked portable ComfyUI and inpaint nodes OpenFill for GIMP installs, in the
  background, and uses it right away.
- Linux installer (`install.sh`): hands PhotoCraft's menu entry to OpenFill, or
  adds a separate one, and can download PhotoCraft itself.

### Changed

- Every result is one step in PhotoCraft's history, an expand included (it
  crops the canvas and adds its layers, then the steps are made into one named
  Generative Expand). Keep after picking a variation leaves it one step, with
  the kept variation alone; Discard takes the result back completely.
- Create as a layer with a selection makes a layer the size of the selection,
  placed on it, as in OpenFill for GIMP.
- A removal's layer is called Object Removal, whichever model made it.

### Fixed

- The FLUX.1 Fill removal LoRA asked for the FLUX.1 Fill Q4 GGUF (another
  6.8 GB) even when the Q6 GGUF or the full FLUX.1 Fill was installed. It now
  runs on whichever FLUX.1 Fill is there.
- Picking the active fill model in the Remove menu ran LaMa instead.
- A busy ComfyUI that did not answer one status request for a moment failed
  the job with "The AI backend is not available".
- Starting a fill without a selection from the page named GIMP's tools.

### Known limits

- Fill with FLUX or Qwen, Expand with every model, and Create work at about
  1024 px on the long side and are scaled up on large images (see the README).
  A second Expand pass at full resolution for SD 1.5 and SDXL is in the code
  but off: it has not been tested on real photos yet.
