# PhotoCraft companion, first end-to-end run (2026-10-21)

PhotoCraft 0.11.1 (Linux x86_64 tarball, SHA-256 checked) on Fedora 44 under Xvfb, ComfyUI 0.40
on an RTX 2070 Max-Q (8 GB). The companion was started with `python -m openfill_photocraft`;
`photocraft/e2e/run_e2e.py` drove its HTTP API exactly as the page does and checked the result
through PhotoCraft's control channel. `report.json` holds every case.

| Case | Result |
|---|---|
| Expand top and bottom by 270 px (DreamShaper 8) | done in 38 s, 8 live preview frames, canvas 1448 x 1626, masked layer, no line at the old edges after the feather fix |
| Generative Fill, 3 variations (RealVisXL V5) | done in 81 s, 25 preview frames, variations ranked best first in a group; the picker shows one, Keep selected deletes the others, the selection is put back |
| Remove object, ellipse around the car (LaMa) | done in 8 s |
| Text to Image as a layer (512 x 384) | done in 34 s |
| Text to Image as a new image, 2 variations | done in 51 s; the document is named after the prompt, the second variation is a hidden layer |

Screenshots `*.jpg` are the PhotoCraft window after each step; `*-full.jpg` the flattened image.
