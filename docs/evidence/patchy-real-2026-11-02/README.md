# OpenFill for Patchy with a real ComfyUI on Windows, Linux and macOS (2026-11-02)

`patchy/e2e/run_real_e2e.py` on each machine, against a Patchy development build with Plugins > Companions
and companion protocol 1 (it reports version 1.24, the last release). The script installs OpenFill
(`python -m openfill_patchy.install`), starts its own Patchy offscreen on `tests/data/test-scene.png`,
starts the companion as Patchy's menu would (`python -m openfill_patchy --no-bar`) and drives it through
its page's API, the calls the panel makes. The companion's settings and the companions folder were kept
in a scratch home on each machine (`--home-root`). Seeds were fixed, so the three runs drew nearly the
same pictures. Every case passed on every machine. On glados it also ran against an AppImage of the
Patchy branch, with OpenFill started by hand as `--connector <the AppImage file>`.

| | Windows 11 (this PC) | Ubuntu 24.04 (glados) | macOS 27.0.1 (studiomac) |
|---|---|---|---|
| Graphics | RTX 5090, `cuda:0` | RTX A6000, `cuda:1` (the RTX 5090 is `cuda:0`; both partly in use by other work) | M2 Ultra, 192 GB, `mps` |
| ComfyUI | 0.39.0, the portable build OpenFill's Download ComfyUI made earlier, found by itself | 0.43.0, git checkout bb878b9e with a venv (torch 2.15.0+cu132), chosen like Choose folder | 0.43.0, git checkout bb878b9e with a venv (torch 2.15.0), chosen like Choose folder |
| Python running OpenFill | Miniconda 3.13.9 | system 3.12.3 (`python3-pil`, `python3-tk` from apt) | ComfyUI's venv, Homebrew 3.13.18 (with `python-tk@3.13`) |
| LaMa | downloaded in 6.5 s | downloaded in 5.2 s, then Install nodes (5.2 s) | downloaded and nodes installed in 10.1 s |
| DreamShaper 8 Inpainting | already there | downloaded in 35.8 s | downloaded in 45.4 s |
| ComfyUI start | 11.6 s | 6.1 s | 19.6 s |
| Remove (LaMa) | 15.1 s, one step | 2.0 s, one step | 4.1 s, one step |
| Fill, 3 variations, pick, Keep | 11.1 s, one step | 4.1 s, one step | 18.1 s, one step |
| Fill, 3 variations, Discard | 4.0 s, no step | 3.1 s, no step | 14.1 s, no step |
| Expand 160 px left | 5.1 s, one step | 3.1 s, one step | 12.1 s, one step |
| Create, 512 x 512 new image | 3.0 s | 1.0 s | 6.0 s |
| Patchy closes | OpenFill stopped in 6.4 s, ComfyUI in 7.4 s | 5.4 s, 6.4 s | 5.4 s, 6.4 s |
| Uninstall | entry removed; settings, models kept; nothing running | same | same |

"One step" means one Undo took the result back and Redo put it back; "no step" means the history was
where it was before the job. Times are from the request to the result in the document. The Windows
Remove includes loading LaMa; on glados the models and nodes came from an earlier run of the same
script that stopped at Remove (LaMa's nodes were not installed yet; the script now presses Install
nodes, as the panel asks). The companion started in 0.5 to 1 s on Windows and Linux, and in 35.5 s on
the Mac: Python's HTTP server looks up the host's name when it starts, and that lookup takes 35 s on
studiomac (`socket.getfqdn`, in the shared page server). OpenFill for Patchy now starts its page
without that lookup (`patchy/openfill_patchy/page.py`); a second Mac run the same day started in 0.5 s
and passed every case again.

The AppImage run (`linux-appimage-report.json`, `linux-appimage.jpg`) matched the native one: Remove
2.0 s, Fill with Keep 4.0 s, Discard 3.1 s, Expand 3.1 s, Create 2.0 s, each one step (Discard none),
OpenFill stopped 6.9 s and ComfyUI 8.9 s after Patchy closed. Started from an AppImage Patchy's
Companions menu (emulated: OpenFill got that Patchy's own environment plus what Patchy adds for a
companion), OpenFill finds no `PATCHY_MCP`, because the AppImage has no connector file beside
`usr/bin/patchy`; it runs the connector as `<the file in APPIMAGE> patchy-mcp --attach`, attached in
0.5 s and stopped when that Patchy closed.

Files: `windows-report.json`, `linux-report.json`, `linux-appimage-report.json`, `macos-report.json`
(home folders replaced by `~`), and `windows.jpg`, `linux.jpg`, `linux-appimage.jpg`, `macos.jpg`: the document before, after Remove, with the second fill
variation shown, after Expand, and the Create image.

Also on 2026-11-02, outside this script:

- The handshake against Patchy 1.24 (the Flatpak installed on glados, which has no companions): OpenFill
  started by hand found no connector on the PATH, used the Flatpak's own (`flatpak run
  --command=patchy-mcp com.rtsoft.patchy --attach`), attached, and stopped after 1 s with "OpenFill needs
  Patchy 1.25 or newer, the first release with Plugins > Companions. Update Patchy: Patchy 1.24 has no
  companion apps."
- The installer on glados with the Flatpak installed wrote the entry into
  `~/.local/share/RTsoft/Patchy/companions` and `~/.var/app/com.rtsoft.patchy/data/RTsoft/Patchy/companions`,
  and `--uninstall` removed both.
- On the Mac, the installer run with Homebrew's `python3` (3.15, no Pillow, pip refuses to add packages)
  now suggests ComfyUI's Python instead of `pip install Pillow`.

On 2026-11-03, against the released Patchy 1.25:

- `run_real_e2e.py` with Patchy from the 1.25 Windows zip passed every case: attach reported 1.25, Remove
  17.1 s, Fill with 3 variations and Keep 10.2 s (one step), Discard 5.0 s (no step), Expand left 5.1 s
  (one step), Create 3.0 s; closing Patchy stopped OpenFill and ComfyUI, and uninstall kept settings and
  models.
- The 1.25 Flatpak on glados: OpenFill started by hand found the Flatpak's connector by itself, attached
  to Patchy 1.25 with companion protocol 1, and stopped with "Patchy was closed; OpenFill stops too." when
  that Patchy closed.

Not checked here: picking OpenFill and Quit OpenFill in Plugins > Companions, the panel itself (Tk) next
to a visible Patchy window, the Windows tether and the keyboard hand-back, Download ComfyUI from the panel
(the Windows ComfyUI already existed), and an AMD card.
