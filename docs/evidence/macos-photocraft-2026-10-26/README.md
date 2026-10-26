# PhotoCraft on Apple Silicon, 2026-10-26

Tested the unreleased checkout on an M3 MacBook Air, 16 GB, macOS 14.7, arm64. PhotoCraft 0.16.0 universal came from its official release, checked against SHA256SUMS. Python 3.13.2, Tk 8.6.15 Aqua, PyTorch 2.14.1 with MPS available, ComfyUI 0.41.0. Used the existing ComfyUI environment and models, with a separate OpenFill profile and localhost port 8193.

## Results

| Operation | Time | Result |
|---|---:|---|
| Fill | 20.1 s | New layer, one history step |
| Remove, LaMa | 4.0 s | New layer, one history step |
| Expand, 32 pixels each edge | 14.1 s | 512 × 384 became 576 × 448, one history step |
| Create | 12.1 s | New 512 × 384 document |

DreamShaper 8 Inpainting used eight steps, one variation, with refinement off. These are functional checks, not quality or speed benchmarks. The exported Expand result has visible edge artifacts at these settings. See [raw results](report.json), [Remove](remove.png), [Expand](expand.png), and [Create](create.png).

A further Fill followed by Undo restored the original layer count. Cancelling a running Fill reached the cancelled state without adding a layer. Opening a filename containing spaces worked. A native Aqua Tk check used the real service and model scanner: Models rendered the installed DreamShaper, LaMa and MAT rows, and Engine and Generation rendered without Tk callback errors. Engine showed the saved ComfyUI folder.

Finder startup exposed three issues, fixed in this checkout: Python architecture under Rosetta, the Aqua return value for borderless windows, and application discovery scanning personal folders. Finder launch then opened PhotoCraft and the native panel successfully.

Both `sh install.sh` and `sh install.sh --uninstall` passed on the Mac. Restart retained the engine folder, eight-step setting and autostart preference. The engine check reported MPS with 16 GB, and Stop shut down the engine it started.

Command-line uninstall removed the OpenFill app bundles and installed code, preserving PhotoCraft, settings and the sizes and modification times of all 39 files under ComfyUI/models. Reinstall preserved settings. Both test ports were closed afterward. PhotoCraft and OpenFill are installed for further testing; other applications were left alone.

## Remaining checks

- Human review of focus, Dock behavior, resizing, and the uninstaller confirmation dialog. The command-line uninstall path passed; its dialog was not clicked through.
- The panel floats independently on macOS. Following PhotoCraft when it moves or minimizes is Windows-only.
- PhotoCraft's screenshot control request timed out. PNG exports worked and are the evidence here.
- Higher-step output quality, large images, multiple variations, sustained memory pressure, and other Apple chips were not covered.

The public 0.2.2 archive does not contain these Mac changes. Nothing was published during this test.

The initial Mac checks used the broader development checkout. The published change excludes cloud and shared-checkpoint features; its separate verification is recorded in [the push checks](../mac-path-uninstall-2026-10-26.md).
