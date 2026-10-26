# GIMP release verification on Apple Silicon, 2026-10-26

MacBook Air M3, 16 GB, macOS 14.7. GIMP 3.2.6, existing ComfyUI 0.41.0 with PyTorch 2.14.1 and MPS. Tested the public OpenFill 2.0.0-beta.6 archive from [its release](https://github.com/beckettharriman/openfill/releases/tag/v2.0.0-beta.6).

Archive SHA-256: `d448c4c0986f91d8f97069f66219c46b2a7fafcbc80b26186002d9bef5954ccd`, matching GitHub's asset digest.

Installed with `sh install.sh --profile-dir <test-profile> --comfy-dir ~/ComfyUI`. The test profile was separate from the user's GIMP profile. Started a new headless GIMP instance and called the registered plug-in procedures through `python-fu-eval`; see [driver.py](driver.py) and [raw report](report.json).

| Operation | Seconds | Result |
|---|---:|---|
| Fill | 38.6 | Success, new masked layer |
| LaMa Remove | 1.9 | Success, new masked layer |
| Expand right by 64 px | 25.5 | Success, 512 × 384 became 576 × 384, new masked layer |
| Create as layer | 24.6 | Success, new layer |

DreamShaper 8, 25 steps, maximum processing side 512, refinement off, one variation, synthetic test scene scaled to 512 × 384. Fill includes engine startup. These are functional timings, not general performance promises. PNGs beside this report are exports from each operation.

The raw report's `ok: false` is a test-driver bug: it searched the status string for SUCCESS, but this GIMP returns the string `3`. A separate run inside the same GIMP confirmed `int(Gimp.PDBStatusType.SUCCESS) == 3`. All four calls returned 3 with no error and added exactly one layer; [validation.json](validation.json) records this check. The unmodified report and executed driver are retained for transparency.

GIMP quit after the test and its engine/supervisor stopped. GIMP emitted GEGL buffer-leak warnings on exit, retained in [gimp-run.log](gimp-run.log). The test driver did not delete its image before exit; this run does not establish a plug-in memory leak.

Uninstall from the isolated profile removed the plug-in and preserved its settings. The user's existing GIMP plug-in/profile was not replaced. Interactive GIMP windows were covered by the [October 16 evidence](../macos-2026-10-16/); this release check exercised the actual procedures headlessly.
