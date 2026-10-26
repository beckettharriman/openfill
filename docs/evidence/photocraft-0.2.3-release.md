# PhotoCraft 0.2.3 release verification

2026-10-26. Release scope: Apple Silicon support, uninstall launchers, the Windows path diagnostic and setup documentation. Cloud and shared-checkpoint work are excluded.

ZIP: `openfill-photocraft-0.2.3.zip`

SHA-256: `058b9603dfda85f51ef8a3ab75ab1f5ccd3bc4d32c7e3f95cb435e1ad515540d`

The same archive was copied to the M3 and its checksum checked before extraction. `sh install.sh` installed version 0.2.3, verified from its installed version file. Finder opened PhotoCraft and the panel. A real DreamShaper Fill passed in 19.2 seconds (eight steps, small selection in a 512 × 384 image). Stop shut down the engine; the test PhotoCraft instance quit. Shell uninstall and reinstall preserved settings. Test ports 7879 and 8193 were closed afterward.

The earlier [isolated source check](mac-path-uninstall-2026-10-26.md) covered all four operations; this release changes the version and documentation, not that application code. Local verification: 657 core tests (four skips), 59 companion tests (one skip), Ruff 0.14.0 lint/format, and git whitespace checks passed.

Native uninstall dialogs still need a human click-through. Mac panel tracking remains Windows-only. The test left OpenFill 0.2.3 installed and closed on the Mac; other applications and the user's GIMP profile were not changed.
