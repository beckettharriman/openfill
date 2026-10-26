# Mac, Windows path and uninstall checks, 2026-10-26

This change contains the Windows long-path diagnostic, PhotoCraft Mac support, uninstall launchers and installation documentation. Cloud providers and shared-checkpoint code are excluded.

- Core suite: 657 tests, four skips. Companion suite: 59 tests, one skip. Ruff 0.14.0 lint, formatting and git whitespace checks passed.
- Path tests cover rejection before engine startup or download, a short junction spelling, and no Linux restriction.
- Installer tests cover Windows shortcut creation and removal, Linux menu removal, Mac app bundles and architecture, spaces in launcher arguments, confirmation branches, and preserving settings. Existing unrelated Mac bundles are refused.
- A test archive from this isolated checkout was installed on the M3 with `sh install.sh`, then started through Finder. All four PhotoCraft tools passed again: Fill 18.2 s, LaMa Remove 3.0 s, Expand 14.1 s, Create 7.0 s. Eight steps, 512 × 384 starting image, one variation. See [the raw result](macos-photocraft-2026-10-26/ship-four-report.json).
- Its model API found the existing DreamShaper checkpoint. Stop shut down its engine and the test PhotoCraft instance quit afterward.
- The broader [Mac session](macos-photocraft-2026-10-26/) also covered Undo, cancellation, native model/settings rendering and uninstall/reinstall. The [GIMP release check](macos-gimp-2026-10-26/) used the public 2.0.0-beta.6 archive.

Native uninstall confirmation dialogs still need a human click-through. Mac panel movement/minimizing does not follow PhotoCraft; that behavior remains Windows-only. These source changes do not replace the existing release archives.
