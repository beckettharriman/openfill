# Reusing ComfyUI's Python, 2026-10-26

Unreleased changes after PhotoCraft 0.2.3. No release number assigned.

The no-Tk panel now opens through Python's default-browser handler. It no longer searches for Edge, Chrome or another installed browser. The same fallback is used on Windows, macOS and Linux.

An explicit `OPENFILL_PYTHON` takes priority even without Tk. The Linux/macOS scripts preserve paths containing spaces and use valid Python in the no-Tk probe; previously the empty Tk check produced a double semicolon and could not succeed. An invalid explicit path produces an error instead of silently selecting another Python.

Validation:

- 63 companion tests passed, 1 skipped, including real shell execution for automatic and explicit no-Tk selection, paths/arguments containing spaces, invalid explicit selection, and default-browser delegation for all three platform branches.
- The Windows command installer ran in an isolated test folder with the existing portable ComfyUI Python. A stub installer recorded the correct interpreter and arguments. No packages or installed application files were changed.
- Ruff checks, formatting and diff checks passed.

The browser-delegation tests mock the browser opener. They do not claim a visual browser test on every OS. The POSIX launcher tests ran on Linux; a fresh macOS no-Tk launch remains to be checked on hardware.

PhotoCraft discovery is already performed at each launch. Standard-location updates should be found. Explicit executable paths, old symlinks and multiple portable copies can keep selecting an older copy. Future control-API compatibility is not guaranteed by executable discovery.
