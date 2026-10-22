# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Inside a GUI GIMP: open every OpenFill window interactively and capture
it, without ever generating, testing the backend or downloading.

Launch (PowerShell, isolated profile, never the user's own GIMP):

    $env:GIMP3_DIRECTORY = 'C:\\AI\\overnight-scratch\\gimp-profile-ux'
    $env:OPENFILL_TEST_UI_FILE = 'C:\\AI\\overnight-scratch\\ux-run\\ui-hooks.json'
    & 'C:\\Users\\...\\GIMP 3\\bin\\gimp-3.2.exe' --new-instance --no-splash `
        --batch-interpreter python-fu-eval `
        -b "exec(open(r'<repo>\\tests\\gimp_batch_ui_screenshots.py').read())"

OPENFILL_TEST_UI_FILE must be set before GIMP starts: the plug-in processes are
spawned by GIMP and inherit its environment, not this script's. This script
rewrites that JSON file before each call (the plug-in re-reads it), so the
hooks differ per dialog: ``autoclose`` answers Cancel after N seconds,
``settings_tab``, ``advanced`` (expanders open), ``mode`` ("remove"),
``fake_variations`` (picker with tinted copies of the selection) and
``progress_demo`` (the real progress window fed with scripted steps),
``expand`` (Generative Expand editor: ``margins``, ``preset``, ``anchor``,
``drag`` [handle, dx, dy] in screen pixels, ``hover``), ``advanced_steps``
([seconds, open] pairs), ``dialog_dump`` / ``dialog_out`` (window size,
prompt box rectangle, status texts and editor state as JSON lines),
``type`` / ``press`` (scripted typing and a key press) and ``dry_run``
(record the procedure config instead of generating). Further hooks used by
the GUI end-to-end driver (docs/evidence/overnight-2026-10-09/gui-e2e):
``values`` (dialog start values), ``click`` ("generate"), ``picker_steps``
(also after a real generation) and ``settings_steps`` / ``settings_out``. OPENFILL_TEST_MENU=1 also
captures the Filters > OpenFill menu (sends keys to this GIMP window only).
OPENFILL_REPO is the checkout to use, OPENFILL_TEST_OUT the screenshot folder.

Each window is captured by a PowerShell helper (written next to the output)
that finds the window by its exact title among windows of processes whose
parent is this GIMP, renders it with PrintWindow (works when the window is
covered), crops the invisible resize border (DWM extended frame bounds),
scales to at most 1600 px wide and saves a PNG.
"""

import json
import os
import subprocess
import sys
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio  # noqa: E402

REPO = os.environ.get("OPENFILL_REPO", r"C:\repos\gimp-stable-boy")
OUT = os.environ.get("OPENFILL_TEST_OUT", os.path.join(REPO, "docs", "screenshots"))
HOOKS = os.environ.get("OPENFILL_TEST_UI_FILE", "")
IMAGE = os.environ.get("OPENFILL_TEST_IMAGE", os.path.join(REPO, "tests", "data", "test-scene.png"))
WORK = os.environ.get("OPENFILL_TEST_WORK", os.path.join(os.environ.get("TEMP", OUT), "openfill-ui-shots"))
os.makedirs(OUT, exist_ok=True)
os.makedirs(WORK, exist_ok=True)
GIMP_PID = os.getppid()
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": GIMP_PID, "hooks_file": HOOKS, "cases": []}

CAPTURE_PS1 = r"""
param([string]$Title, [string]$Out, [int]$ParentPid, [string]$DelaysMs = "2500", [int]$Screen = 0, [int]$ClickCancel = 0,
      [int]$TimeoutSec = 90,
      [int]$MaxWidth = 1600)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SbWin {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, int x, int y, uint d, UIntPtr e);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out RECT r, int size);
  public static IntPtr Find(string title, uint[] pids) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      if (sb.ToString() != title) return true;
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (Array.IndexOf(pids, pid) < 0) return true;
      found = h; return false;
    }, IntPtr.Zero);
    return found;
  }
}
"@
[SbWin]::SetProcessDPIAware() | Out-Null
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$hwnd = [IntPtr]::Zero
while ((Get-Date) -lt $deadline -and $hwnd -eq [IntPtr]::Zero) {
  $pids = @([uint32]$ParentPid) + @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentPid" | ForEach-Object { [uint32]$_.ProcessId })
  $hwnd = [SbWin]::Find($Title, [uint32[]]$pids)
  if ($hwnd -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 250 }
}
if ($hwnd -eq [IntPtr]::Zero) { "NOT FOUND: $Title"; exit 2 }
$start = Get-Date
$i = 0
foreach ($d in ($DelaysMs -split ",")) {
  $wait = [int]$d - ((Get-Date) - $start).TotalMilliseconds
  if ($wait -gt 0) { Start-Sleep -Milliseconds $wait }
  $r = New-Object SbWin+RECT; [SbWin]::GetWindowRect($hwnd, [ref]$r) | Out-Null
  $f = New-Object SbWin+RECT
  if ([SbWin]::DwmGetWindowAttribute($hwnd, 9, [ref]$f, 16) -ne 0) { $f = $r }
  $w = $r.R - $r.L; $h = $r.B - $r.T
  if ($w -le 0 -or $h -le 0) { "EMPTY RECT: $Title"; exit 3 }
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  if ($Screen -eq 1) { $g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size); $ok = "screen" }
  else { $hdc = $g.GetHdc(); $ok = [SbWin]::PrintWindow($hwnd, $hdc, 2); $g.ReleaseHdc($hdc) }
  $g.Dispose()
  $focused = ([SbWin]::GetForegroundWindow() -eq $hwnd)
  $crop = New-Object System.Drawing.Rectangle ($f.L - $r.L), ($f.T - $r.T), ($f.R - $f.L), ($f.B - $f.T)
  $img = $bmp.Clone($crop, $bmp.PixelFormat); $bmp.Dispose()
  if ($img.Width -gt $MaxWidth) {
    $nh = [int]($img.Height * $MaxWidth / $img.Width)
    $small = New-Object System.Drawing.Bitmap $MaxWidth, $nh
    $g2 = [System.Drawing.Graphics]::FromImage($small)
    $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g2.DrawImage($img, 0, 0, $MaxWidth, $nh); $g2.Dispose(); $img.Dispose(); $img = $small
  }
  $name = if ($i -eq 0) { $Out } else { $Out -replace "\.png$", "-$i.png" }
  $img.Save($name, [System.Drawing.Imaging.ImageFormat]::Png)
  "saved $name $($img.Width)x$($img.Height) printwindow=$ok rect=$($r.L),$($r.T),$w,$h foreground=$focused"
  $img.Dispose()
  $i++
}
if ($ClickCancel -eq 1) {
  # a real mouse click on the Cancel button (bottom right of the progress window)
  $r = New-Object SbWin+RECT; [SbWin]::GetWindowRect($hwnd, [ref]$r) | Out-Null
  [SbWin]::SetCursorPos($r.R - 70, $r.B - 30) | Out-Null; Start-Sleep -Milliseconds 300
  [SbWin]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 80; [SbWin]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 700
  $w = $r.R - $r.L; $h = $r.B - $r.T
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc(); [SbWin]::PrintWindow($hwnd, $hdc, 2) | Out-Null; $g.ReleaseHdc($hdc); $g.Dispose()
  $name = $Out -replace "\.png$", "-clicked.png"
  $bmp.Save($name, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
  "clicked Cancel, saved $name foreground=$([SbWin]::GetForegroundWindow() -eq $hwnd)"
}
"""

# Optional (OPENFILL_TEST_MENU=1): open Filters > OpenFill in the GIMP window with the
# keyboard and capture the screen area. Keys are sent only after checking
# that the foreground window belongs to this GIMP; the screen must be
# unlocked. "{UP} {UP} {RIGHT}" assumes AI is the second-to-last entry of
# the Filters menu (GIMP 3.2.6 with no other plug-in submenus after it).
MENU_PS1 = r"""
param([int]$GimpPid, [string]$Out, [string]$Keys = "{UP} {UP} {RIGHT}", [int]$CropW = 1000, [int]$CropH = 860)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SbMenu {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static void CloseWelcome(uint pid) {
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      uint wp; GetWindowThreadProcessId(h, out wp);
      if (wp != pid) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      if (sb.ToString().StartsWith("Welcome")) PostMessage(h, 0x10, IntPtr.Zero, IntPtr.Zero);
      return true;
    }, IntPtr.Zero);
  }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  public static IntPtr FindMain(uint pid) {
    IntPtr found = IntPtr.Zero; int best = 0;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      uint wp; GetWindowThreadProcessId(h, out wp);
      if (wp != pid) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      if (!sb.ToString().Contains("GIMP")) return true;
      RECT r; GetWindowRect(h, out r); int a = (r.R - r.L) * (r.B - r.T);
      if (a > best) { best = a; found = h; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
[SbMenu]::SetProcessDPIAware() | Out-Null
[SbMenu]::CloseWelcome([uint32]$GimpPid); Start-Sleep -Milliseconds 800
$h = [SbMenu]::FindMain([uint32]$GimpPid)
if ($h -eq [IntPtr]::Zero) { "no GIMP window for pid $GimpPid"; exit 2 }
# Alt press/release trick so SetForegroundWindow is allowed, then verify before sending anything
[SbMenu]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [SbMenu]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[SbMenu]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 700
$fg = [SbMenu]::GetForegroundWindow()
[uint32]$fgpid = 0; [SbMenu]::GetWindowThreadProcessId($fg, [ref]$fgpid) | Out-Null
if ($fgpid -ne $GimpPid) { "foreground is pid $fgpid, not GIMP ${GimpPid} - not sending keys"; exit 3 }
$r = New-Object SbMenu+RECT; [SbMenu]::GetWindowRect($h, [ref]$r) | Out-Null
$ws = New-Object -ComObject WScript.Shell
$ws.SendKeys("%r")
Start-Sleep -Milliseconds 1200
if ($Keys) { foreach ($k in ($Keys -split " ")) { $ws.SendKeys($k); Start-Sleep -Milliseconds 400 }; Start-Sleep -Milliseconds 1000 }
$w = [Math]::Min($r.R - $r.L, $CropW); $hh = [Math]::Min($r.B - $r.T, $CropH)
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size)
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"saved $Out rect=$($r.L),$($r.T),$w,$hh"
$ws.SendKeys("{ESC}"); Start-Sleep -Milliseconds 200; $ws.SendKeys("{ESC}"); Start-Sleep -Milliseconds 200; $ws.SendKeys("{ESC}")

"""


def capture_menu(out_name):
    script = os.path.join(WORK, "capture-menu.ps1")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(MENU_PS1)
    powershell = os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    res = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            "-GimpPid",
            str(GIMP_PID),
            "-Out",
            os.path.join(OUT, out_name),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    report["cases"].append({"case": "menu", "capture": (res.stdout + res.stderr).strip(), "capture_rc": res.returncode})


def capture(title, out_name, delays_ms="2500", screen=False, click_cancel=False):
    script = os.path.join(WORK, "capture-window.ps1")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CAPTURE_PS1)
    out = os.path.join(OUT, out_name)
    powershell = os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    return subprocess.Popen(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            "-Title",
            title,
            "-Out",
            out,
            "-ParentPid",
            str(GIMP_PID),
            "-DelaysMs",
            delays_ms,
            "-Screen",
            "1" if screen else "0",
            "-ClickCancel",
            "1" if click_cancel else "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def set_hooks(**hooks):
    if not HOOKS:
        raise RuntimeError("OPENFILL_TEST_UI_FILE must be set in the environment GIMP was started with")
    with open(HOOKS, "w", encoding="utf-8") as fh:
        json.dump(hooks, fh)


extra_clicked = "progress-window-cancel-clicked.png"


def run_case(
    name,
    proc_name,
    title,
    out_name,
    hooks,
    image=None,
    delays_ms="2500",
    setup=None,
    names=(),
    screen=False,
    click_cancel=False,
):
    case = {"case": name, "procedure": proc_name, "title": title, "hooks": hooks}
    try:
        set_hooks(**hooks)
        cap = capture(title, out_name, delays_ms, screen, click_cancel) if out_name else None
        proc = Gimp.get_pdb().lookup_procedure(proc_name)
        config = proc.create_config()
        config.set_property("run-mode", Gimp.RunMode.INTERACTIVE)
        if image is not None:
            config.set_property("image", image)
            config.set_core_object_array("drawables", image.get_selected_layers() or image.get_layers()[:1])
        if setup:
            setup(config)
        t0 = time.time()
        result = proc.run(config)
        case["status"] = str(result.index(0))
        case["seconds"] = round(time.time() - t0, 1)
        if cap is not None:
            try:
                output, _ = cap.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                cap.kill()
                output = "capture timed out"
            case["capture"] = (output or "").strip()
            case["capture_rc"] = cap.returncode
        for index, extra in enumerate(names, start=1):  # later shots: <out>-<n>.png -> given names
            src = os.path.join(OUT, out_name[:-4] + f"-{index}.png")
            if extra == "-clicked":
                src = os.path.join(OUT, out_name[:-4] + "-clicked.png")
                extra = extra_clicked
            if os.path.exists(src):
                os.replace(src, os.path.join(OUT, extra))
    except Exception as err:  # noqa: BLE001
        case["error"] = f"{type(err).__name__}: {err}"
        case["traceback"] = traceback.format_exc()
    report["cases"].append(case)
    print(f"[sb-ui] {json.dumps(case, default=str)[:400]}")


try:
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(IMAGE))
    Gimp.Display.new(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 420, 300, 260, 200)
    Gimp.displays_flush()
    time.sleep(2)
    if os.environ.get("OPENFILL_TEST_MENU") == "1":
        capture_menu("filters-ai-menu.png")
    fill, expand, txt = (
        "python-fu-openfill-generative-fill",
        "python-fu-openfill-generative-expand",
        "python-fu-openfill-text-to-image",
    )
    settings = "python-fu-openfill-settings"

    def set_prompt(text):
        return lambda config: config.set_property("prompt", text)

    # Keyboard behaviour, without generating: Enter in the prompt answers
    # Generate (dry_run records what reached the procedure config instead of
    # calling the backend, and the prompt history is updated as in real use);
    # Escape cancels. Three prompts are entered so Recent prompts has entries.
    dry_out = os.path.join(WORK, "dry-run.json")
    for prompt in (
        "a stone fountain in the middle of the lawn",
        "a small wooden cabin with a red roof",
        "a red door with brass handles",
    ):
        if os.path.exists(dry_out):
            os.remove(dry_out)
        run_case(
            "keys-enter",
            fill,
            "Generative Fill",
            None,
            {"autoclose": 12, "type": prompt, "press": "Return", "dry_run": True, "dry_run_out": dry_out},
            image,
        )
        try:
            with open(dry_out, encoding="utf-8") as fh:
                report["cases"][-1]["dry_run"] = json.load(fh)
        except OSError as err:
            report["cases"][-1]["dry_run"] = f"missing: {err}"
    run_case("keys-escape", fill, "Generative Fill", None, {"autoclose": 12, "press": "Escape"}, image)

    run_case(
        "fill",
        fill,
        "Generative Fill",
        "fill-dialog.png",
        {"autoclose": 7},
        image,
        setup=set_prompt("a weathered wooden bench, soft afternoon light"),
    )
    run_case(
        "fill-recent",
        fill,
        "Generative Fill",
        "fill-dialog-recent-prompts.png",
        {"autoclose": 7, "open_recent": True},
        image,
        delays_ms="3000",
        screen=True,
    )
    run_case(
        "fill-advanced", fill, "Generative Fill", "fill-dialog-advanced.png", {"autoclose": 7, "advanced": True}, image
    )
    run_case(
        "fill-remove", fill, "Generative Fill", "fill-dialog-remove.png", {"autoclose": 7, "mode": "remove"}, image
    )
    run_case("expand", expand, "Generative Expand", "expand-dialog.png", {"autoclose": 7}, image, setup=set_prompt(""))

    # Layout checks without pixels: every dialog below also appends its window
    # size, prompt box rectangle, status texts and (Expand) editor state to
    # dialog-state.jsonl; dry_run guarantees that nothing is generated even if
    # Generate were answered.
    state_out = os.path.join(WORK, "dialog-state.jsonl")
    if os.path.exists(state_out):
        os.remove(state_out)

    def dumped(dump, **hooks):
        return {"dialog_dump": dump, "dialog_out": state_out, "dry_run": True, **hooks}

    # Fill: Advanced closed, opened, closed again in one window. The prompt box
    # height and the window width must be the same in all three; the window
    # top must not move; the height grows only while Advanced is open.
    run_case(
        "fill-advanced-toggle",
        fill,
        "Generative Fill",
        "fill-dialog-toggle.png",
        dumped([1.2, 2.7, 4.2], autoclose=5, advanced_steps=[[1.6, True], [3.1, False]]),
        image,
        delays_ms="1300,2800,4300",
        names=("fill-dialog-toggle-advanced.png", "fill-dialog-toggle-closed.png"),
    )
    run_case(
        "fill-removal-note",
        fill,
        "Generative Fill",
        "fill-dialog-removal-model-note.png",
        dumped([1.5], autoclose=4, values={"model": "active"}, prompt_free="MAT - prompt-free removal"),
        image,
    )
    Gimp.Selection.none(image)
    run_case(
        "fill-no-selection",
        fill,
        "Generative Fill",
        "fill-dialog-no-selection.png",
        dumped([1.5], autoclose=4),
        image,
    )
    run_case(
        "fill-empty-prompt",
        fill,
        "Generative Fill",
        "fill-dialog-empty-prompt.png",
        dumped([3.0], autoclose=5, click="generate", values={"prompt": ""}),
        image,
        delays_ms="3200",
    )
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 420, 300, 260, 200)

    expand_cases = (
        # (case, screenshot, expand hooks, start margins)
        ("expand-default", "expand-editor.png", {}, None),
        ("expand-zero", "expand-editor-zero.png", {}, {"left": 0, "top": 0, "right": 0, "bottom": 0}),
        ("expand-16-9-left", "expand-editor-16-9-left.png", {"preset": "16:9", "anchor": "left"}, None),
        ("expand-9-16-center", "expand-editor-9-16.png", {"preset": "9:16", "anchor": "center"}, None),
        ("expand-1-1-bottom-right", "expand-editor-1-1-corner.png", {"preset": "1:1", "anchor": "bottom-right"}, None),
        ("expand-original", "expand-editor-original.png", {"preset": "original"}, None),
        (
            "expand-drag",
            "expand-editor-drag.png",
            {
                "margins": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                "drag": [["right", 60, 0], ["bottom-left", -40, 30]],
            },
            None,
        ),
        ("expand-hover", "expand-editor-hover.png", {"hover": "top-right"}, None),
        ("expand-huge", "expand-editor-huge.png", {"margins": {"left": 4096, "right": 4096}}, None),
    )
    for case, shot, editor_hooks, start in expand_cases:
        hooks = dumped([2.0], autoclose=4, expand=editor_hooks)
        if start is not None:
            hooks["values"] = start
        run_case(case, expand, "Generative Expand", shot, hooks, image, setup=set_prompt(""))
    run_case(
        "expand-advanced-toggle",
        expand,
        "Generative Expand",
        "expand-editor-toggle.png",
        dumped([1.2, 2.7, 4.2], autoclose=5, advanced_steps=[[1.6, True], [3.1, False]]),
        image,
        delays_ms="1300,2800,4300",
        names=("expand-editor-toggle-advanced.png", "expand-editor-toggle-closed.png"),
        setup=set_prompt(""),
    )
    run_case(
        "expand-generate-zero",
        expand,
        "Generative Expand",
        "expand-editor-generate-zero.png",
        dumped([3.0], autoclose=5, click="generate", values={"left": 0, "top": 0, "right": 0, "bottom": 0}),
        image,
        delays_ms="3200",
        setup=set_prompt(""),
    )
    try:
        with open(state_out, encoding="utf-8") as fh:
            report["dialog_state"] = [json.loads(line) for line in fh if line.strip()]
    except OSError as err:
        report["dialog_state"] = f"missing: {err}"
    run_case(
        "txt2img",
        txt,
        "Text to Image",
        "text-to-image-dialog.png",
        {"autoclose": 7},
        image,
        setup=set_prompt("a lighthouse on a cliff at dusk, oil painting"),
    )
    run_case(
        "settings-general",
        settings,
        "Generative Fill Settings",
        "settings-general.png",
        {"autoclose": 7, "settings_steps": [[0.8, "tab", 0]]},
    )
    run_case(
        "settings-advanced",
        settings,
        "Generative Fill Settings",
        "settings-general-advanced.png",
        {"autoclose": 7, "advanced": True, "settings_steps": [[0.8, "tab", 0]]},
    )
    run_case(
        "settings-models",
        settings,
        "Generative Fill Settings",
        "settings-models.png",
        {"autoclose": 8, "settings_tab": "models"},
        delays_ms="3500",
    )
    run_case(
        "settings-models-token",
        settings,
        "Generative Fill Settings",
        "settings-models-token.png",
        {"autoclose": 8, "settings_tab": "models", "select_model": "flux1-fill-dev"},
        delays_ms="3500",
    )
    run_case(
        "progress",
        fill,
        "OpenFill - Generative Fill",
        "progress-window-start.png",
        {"progress_demo": 11},
        image,
        delays_ms="1200,7500",
        names=("progress-window.png", "-clicked"),
        click_cancel=True,
    )
    run_case(
        "picker",
        fill,
        "Choose a variation - Generative Fill",
        "variations-picker.png",
        {"autoclose": 7, "fake_variations": 3},
        image,
    )
    report["cases"][-1]["undo_enabled_after"] = image.undo_is_enabled()

    # Picker actions on fake variation layers (no backend): pick #3 and keep,
    # keep all, discard. The layer tree is recorded after each.
    def tree(img):
        out = []
        for layer in img.get_layers():
            entry = {"name": layer.get_name(), "visible": layer.get_visible()}
            if layer.is_group():
                entry["children"] = [{"name": c.get_name(), "visible": c.get_visible()} for c in layer.get_children()]
            out.append(entry)
        return out

    for steps in (["3", "keep"], ["keep_all"], ["discard"]):
        before = tree(image)
        run_case(
            "picker-" + "-".join(steps),
            fill,
            "Choose a variation - Generative Fill",
            None,
            {"autoclose": 15, "fake_variations": 3, "picker_steps": steps},
            image,
        )
        report["cases"][-1]["layers_before"] = before
        report["cases"][-1]["layers_after"] = tree(image)
        # the picker freezes the undo stack while previewing; it must be thawed again
        report["cases"][-1]["undo_enabled_after"] = image.undo_is_enabled()
    report["ok"] = all("error" not in c for c in report["cases"])
except Exception as err:  # noqa: BLE001
    report["ok"] = False
    report["error"] = f"{type(err).__name__}: {err}"
    report["traceback"] = traceback.format_exc()
    print(report["traceback"], file=sys.stderr)
finally:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(WORK, "ui-screenshots-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    if HOOKS:
        set_hooks()
    if os.environ.get("OPENFILL_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
