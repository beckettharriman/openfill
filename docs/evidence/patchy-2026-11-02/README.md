# OpenFill for Patchy, end to end with a stand-in generator (2026-11-02)

Windows 11 Pro (10.0.26300), Patchy 1.24, an opt-dev build of Patchy's main branch (built 2026-11-02 17:02;
main was at 089cb12ea), offscreen (`QT_QPA_PLATFORM=offscreen`), Python 3.13.9 with Pillow 12.1.1. No
ComfyUI, models or graphics card: `patchy/tests/stub_backend.py` stood in for the generator. A fill or
an expand comes back as its context inverted, each further variation 8 levels darker, and text to
image as a flat colour. So these runs check what OpenFill does in Patchy (layers, masks, history,
events), not what a model draws.

`run_e2e-report.json`: `python patchy/e2e/run_e2e.py --patchy PATH/patchy.exe`. The script started its
own Patchy on a 256 x 192 test image (its own settings folder and connector endpoint), attached the
companion through `patchy-mcp --attach`, and ran the companion's own service jobs. It checked the
document through the connector (`get_state` and small read-only scripts). ALL DONE, 11 of 11.

| Case | Result |
|---|---|
| `attach` | connected to the Patchy it started (version 1.24, events offered) |
| `selection_event`, `watcher_follows_selection` | a soft-edged selection set by script reached the companion as a `selectionChanged` event (65, 49, 94 x 62) and the panel's watcher |
| `read_adds_no_step` | reading the image and selection left the history as it was; the selection's soft edge read 0, 64, 128, 192, 255 |
| `fill_one_step` | "Generative Fill: a red door", a layer with a raster mask (feather 3), the pixel at (112, 80) inverted from 200, 100, 50 to 55, 155, 205; one Undo removed it |
| `variations_keep_one_step` | three variations in a group, only the first visible; the picker showed the third, Keep left one masked layer; one Undo removed it |
| `discard_no_step` | two variations, the picker, Discard: the history back where it was, no layer left |
| `expand_one_step` | 128 px on the right in two rounds: 256 to 384 px wide, two Generative Expand layers (the check requires a mask on each; the report lists names and positions), one step |
| `expand_left_background` | 64 px on the left of a new 200 x 150 document with a blue Background: 264 px wide, the Background at 0, 0, 264 x 150, its new area filled with the image's mean colour (51, 102, 204) and its old pixels moved 64 px right; one Undo back to 200 x 150 |
| `txt2img_new_image` | a new document "Text to Image a fox", 680 x 512 (OpenFill's working size for the 128 x 96 asked), two variations as layers, the second hidden |
| `exchange_folder_empty` | no PNG files left in the exchange folder |

The images are the document as Patchy composites it after each job (Patchy ran offscreen, so these are
not screenshots): `0-before.png`, `1-fill.png`, `2-variation-3.png` (the picker on the third variation,
which after ranking is the stand-in's undarkened one, so it matches `1-fill.png`), `3-expand.png`,
`4-expand-left.png` (the generated layers cover the new area) and `5-txt2img.png`.

Unit tests the same day: `python -m unittest patchy/tests/test_companion.py`, 24 tests, OK.

Not verified here: a real ComfyUI with real models, picking OpenFill in Plugins > Companions and the
`companionCommand` that brings the panel back, the panel next to a visible Patchy window (the Windows
tether; offscreen Patchy reports a window handle of 1), and macOS and Linux.
