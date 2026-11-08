# Changelog

All notable changes to OpenFill for PhotoCraft are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version numbers below (0.2.x) are OpenFill for PhotoCraft's own, not PhotoCraft's.
Each entry from now on says which PhotoCraft versions it works with.

## [Unreleased] - OpenFill for PhotoCraft · works with PhotoCraft 0.12 to 0.20

Working in PhotoCraft:
- Fill, Remove, Expand and Create no longer flash OpenFill's temporary documents in PhotoCraft. The results come in as one PSD with their masks and group already in it, so there is one temporary document per job instead of one per layer and mask, and it is never worked on in view. PhotoCraft is back on your image right after it opens.
- The copy OpenFill reads your image from is shown at your zoom and position, and from PhotoCraft 0.18 on the selection is read from that same copy, so the black and white mask document is gone. Its tab still shows for about a second, because PhotoCraft can only save the document in front.
- Expand no longer shows the bigger canvas empty (white) while the result is finished: the canvas grows when the new layers go in.
- A failed or cancelled Expand leaves the canvas and layers as they were, also when it fails in a later round, unless you changed the image in between.
- A result now always lands on top of the layer stack. Before, a job after a Fill with several variations put its layer inside that Fill's group.
- OpenFill only ever saves or closes its own temporary documents, found by their own names. Before, clicking your own tab at the wrong moment during a job could get your image saved into OpenFill's folder or closed without asking.
- The result goes into the right image when two open images share a name and another tab closes during the job.
- Bigger selections on big images: the grown mask is made once per job, in about half a second on a 24 MP image instead of several seconds per variation.

PhotoCraft versions:
- Tested on PhotoCraft 0.17.0, 0.18.0, 0.19.0, 0.20.1 and 0.20.2 (install, Fill, Remove, Expand and Create). No changes were needed, so the supported range is now PhotoCraft 0.12 to 0.20.
- The Engine section of the panel shows an About line: "OpenFill for PhotoCraft 0.2.x · works with PhotoCraft 0.12 to 0.20".
- READMEs, badges and install docs name OpenFill's version and the PhotoCraft range separately, so 0.2.x can't be read as a PhotoCraft version.
- Linux: when the installer downloads PhotoCraft for you, it checks the download against PhotoCraft's checksum list, never the signature file that releases carry from 0.20.2 on.

Where images are made:
- Add an optional cloud service under Engine: OpenRouter or any OpenAI-compatible image API, with your own key. Off unless you pick it; the panel then says your image is sent to it, and it is never used as a fallback. Untested against the real services so far. See docs/cloud.md.
- Add Remote GPU in the gear's Engine section: use ComfyUI on another computer of yours, over Tailscale or SSH, with the OpenFill GPU server's pairing code. See docs/remote-gpu.md.
- Models from another ComfyUI: the gear's Engine section takes the folder of another ComfyUI, such as Krita AI Diffusion's, and OpenFill uses its models read only. Nothing is downloaded, deleted or installed there (#6).
- Your own FLUX.1 Fill file reuses the FLUX text encoders and VAE already in ComfyUI, under other names, formats or the `text_encoders` folder, instead of downloading them again (#8).

Engine:
- When ComfyUI crashes or closes during a job, say so right away instead of after 90 seconds.
- The status line says "Stopped: Generate restarts it" after ComfyUI stops, instead of "Ready".
- Error messages point to OpenFill's settings in PhotoCraft, not to GIMP's menus and profile folder.
- Intel Arc graphics, untested: on Windows the Download ComfyUI card gets ComfyUI's version for Intel for an Arc card or a Core Ultra chip with Arc graphics, and says it is untested. On Linux the card points to the PyTorch for Intel steps.
- The setup card's help link goes to the Intel steps on Intel graphics.
- Mac model menu: a menu closed while it is open is removed only after macOS has dismissed it.

Installing:
- Windows: the installer finds python.org's 3.14 per-user, all-users and install manager Pythons and adds Pillow to them. It works from a folder with an apostrophe or ! in its name.
- With no usable Python, the installers say what to install: python.org on Windows and Mac, the dnf, apt or pacman line on Linux.
- Mac: the launchers never run macOS's own Python, which can pop up a developer tools dialog, and say so when OPENFILL_PYTHON points to it.
- Uninstall docs point to the Uninstall OpenFill for PhotoCraft menu entries.

## [0.2.4] - 2026-10-26

- Fix Mac prompt typing by keeping the expanded panel keyboard-focusable.
- Use the native Mac model menu and restore focus after selection or cancellation.
- Open the no-Tk panel in the default browser on Windows, macOS and Linux.
- Let an explicitly selected ComfyUI Python run OpenFill without installing another Python.
- Fix Linux/macOS interpreter fallback and paths containing spaces; document setup and PhotoCraft update discovery.

## [0.2.3] - 2026-10-26

- Detect long Windows paths before ComfyUI startup or download and explain the shorter-folder fix.
- Add PhotoCraft installation on Apple Silicon, tested with all four tools on an M3 MacBook Air.
- Fix Finder Python architecture, Aqua panel startup and PhotoCraft discovery on macOS.
- Add uninstall launchers on Windows, Linux and macOS, preserving settings and models.
- Document downloads, model sizes, existing-engine limits and tested Mac setup.

## [0.2.2] - 2026-10-25

### Added

- AMD graphics cards. I don't have an AMD card, so this is untested on real
  AMD hardware so far; please tell me how it goes.
  - On Windows, **Download ComfyUI** gets ComfyUI's own version for AMD cards
    (ROCm 7.2.1, 1.6 GB) for a Radeon RX 7000 or RX 9000, Radeon PRO W7000,
    or Ryzen AI 300 or Max chip, from the same ComfyUI release and checked the
    same way as the NVIDIA one. The setup card says which one it gets. For an
    older Radeon it says to follow the AMD steps in the install guide and
    choose the folder.
  - On Linux, a ComfyUI with PyTorch for ROCm works through **Choose folder**,
    and **How to install** opens the AMD steps when the card is AMD.
  - Models shows an AMD card's name and graphics memory
    ("RX 7900 XT · 20 GB VRAM · ..."), and on an AMD card the GGUF models say
    that they have been reported to give only noise on some Radeon cards.
- The README says where the model manager is: the gear icon, Models.

## [0.2.1] - 2026-10-25

### Fixed

- Keep could lose the variations. Clicking in PhotoCraft (to deselect, say)
  while Keep was still working made it fail with "select.modify.feather is not
  available right now: no selection", and the variations were gone. Keep now
  makes the kept layer from the job's own selection first and only then takes
  the variations away, a click on the way is tried again, and if Keep still
  fails the variations stay as they were and Keep can be pressed again. The
  same goes for the result a Fill, Remove or Expand adds at the end.
- Remove with LaMa failed the first time on a ComfyUI you already had, with
  "needs the custom node pack(s) openfill-nodes; press Install nodes in the
  Models tab", until another job had started ComfyUI. OpenFill now puts its
  own node pack in place before ComfyUI starts, as it does for the ComfyUI it
  downloads for you.

## [0.2.0] - 2026-10-25

First public release.

### Added

- Generative Fill, Remove object, Generative Expand and Text to Image for
  PhotoCraft 0.12 to 0.16, running on a local ComfyUI with OpenFill's engine (the
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
- PhotoCraft is started without its own control and automation variables
  (`PHOTOCRAFT_CONTROL_TOKEN` and the others) in the environment. A token
  exported next to OpenFill's token file stopped PhotoCraft from starting.
- Image names that start with `-` reach PhotoCraft as files, not as options
  (PhotoCraft refuses an option it does not know).

### Known limits

- Fill with FLUX or Qwen, Expand with every model, and Create work at about
  1024 px on the long side and are scaled up on large images (see the README).
  A second Expand pass at full resolution for SD 1.5 and SDXL is in the code
  but off: it has not been tested on real photos yet.
