# OpenFill for PhotoCraft on PhotoCraft 0.16.0 (2026-10-25)

Linux (Fedora 44, RTX 2070 Max-Q 8 GB), Xvfb, the official `photocraft-0.16.0-linux-x86_64.tar.gz`
(SHA-256 7c17f3633ebf7dabf27628e2d2637d918c7c2a5b163b8b90347a78acddf63608, as in its SHA256SUMS.txt),
the companion from main, ComfyUI with `--lowvram`. Fills and Create used DreamShaper 8 Inpainting,
Remove used LaMa.

- `run_e2e-report.json`: `photocraft/e2e/run_e2e.py --model dreamshaper-8-inpainting`. Expand, Fill
  with 3 variations and the picker, Remove, Create as a layer and as an image. All done.
- `run_bar_e2e-report.json`: `photocraft/e2e/run_bar_e2e.py`. Filter > Plug-ins > OpenFill, the
  panel, Fill, Keep, Remove, Expand 16:9, Create, hide and the strip. 10 of 10.
- `extra-undo-cancel-report.json` (`e2e_extra.py`): Fill with 1 variation, Fill with 3 and Keep, Fill
  with 3 and Discard, Remove, Expand, Create as a layer, each one history step that one Undo takes
  back; Create as an image (a new document; 512 x 384 asked, the SD 1.5 working size 680 x 512 made);
  Cancel leaves the document as it was.
- `smoke-<version>.json` (`smoke_version.py`): PhotoCraft 0.12.0, 0.13.0, 0.14.0, 0.15.1 and 0.16.0
  started by the launcher with an image on the command line, the menu entry (its history step
  removed), Remove with LaMa (one layer, one step) and one Undo. `menu_pick_reached_companion` reads
  a value only the page sets, so it is false with the panel up; the bar test checks the pick.
