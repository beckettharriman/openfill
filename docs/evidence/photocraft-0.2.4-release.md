# PhotoCraft 0.2.4 release verification

2026-10-26. Scope: reuse ComfyUI Python, default-browser fallback, Mac native typing and model-menu focus. No model-generation changes.

ZIP: `openfill-photocraft-0.2.4.zip`

SHA-256: `32609602e37f89fef9e24b260cee695a10ba12e47ff20219c5d693c5d6016132`

The same ZIP was copied to the M3, its checksum compared, and its installer run with the existing ComfyUI Python. The installed package reported version 0.2.4. Its actual PromptBox and Select classes passed native-key input and twelve model-menu select/cancel cycles, with no retained grab and working native input afterward. See [the recorded result](macos-input-2026-10-26/installed-0.2.4.json).

The earlier no-Tk integration check used a separate PhotoCraft instance on the same M3, served the fallback page and authenticated state API, and closed that test instance. The Windows command installer also selected portable ComfyUI Python without Tk in an isolated test.

Local checks: 657 core tests (four skips), 65 companion tests (one skip), Ruff 0.14.0 lint/format and whitespace checks passed.

The Mac keeps a standard title bar on the expanded panel for native keyboard focus. The menu change addresses focus loss, not every possible backend stall. LaMa smoothing and generation quality are unchanged. No new image generation was required for this UI/setup release.
