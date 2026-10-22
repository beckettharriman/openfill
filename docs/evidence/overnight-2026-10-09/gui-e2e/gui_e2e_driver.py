# Stable Boy - generative fill for GIMP 3
# SPDX-License-Identifier: Apache-2.0
"""GUI end-to-end driver, executed INSIDE a GUI GIMP 3.2 (--new-instance,
isolated profile) by the python-fu-eval batch interpreter. Runs real
generations through the INTERACTIVE procedures with the plug-in's test hooks
(scripted typing, scripted picker and settings steps), watches the progress
window with user32 (IsWindowVisible on its HWND), captures windows, exports
composites and records the layer tree. Not a unit test.

Environment: GIMP3_DIRECTORY (test profile), SB_TEST_UI_FILE (hooks JSON,
set before GIMP starts), SB_E2E_OUT (raw output folder), SB_E2E_TASKS
(comma list of fill,expand,settings,remove), SB_E2E_MODEL.
"""

import ast
import json
import os
import subprocess
import time
import traceback

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio  # noqa: E402

REPO = os.environ.get("SB_REPO", r"C:\repos\gimp-stable-boy")
OUT = os.environ["SB_E2E_OUT"]
HOOKS = os.environ["SB_TEST_UI_FILE"]
TASKS = os.environ.get("SB_E2E_TASKS", "fill,expand,settings,remove").split(",")
PHOTOS = r"C:\AI\overnight-scratch\photos"
MODEL = os.environ.get("SB_E2E_MODEL", "juggernaut-xl-v9")
GIMP_PID = os.getppid()
CONFIG = os.path.join(Gimp.directory(), "stable-boy", "config.json")
LOG_PATH = os.path.join(Gimp.directory(), "stable-boy", "stable-boy.log")
POWERSHELL = os.path.join(
    os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
)
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
os.makedirs(OUT, exist_ok=True)
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "gimp_pid": GIMP_PID, "model": MODEL, "tasks": {}}

# the window capture helper of the screenshot driver (PrintWindow or screen copy)
with open(os.path.join(REPO, "tests", "gimp_batch_ui_screenshots.py"), encoding="utf-8") as _fh:
    _src = _fh.read()
CAPTURE_PS1 = next(
    n.value.value
    for n in ast.parse(_src).body
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CAPTURE_PS1"
)

# Polls every 0.5 s for the plug-in's own windows (visible or not) of GIMP and
# its child processes and logs IsWindowVisible / IsIconic / rect per HWND. The
# first time a progress window is visible, its screen area is copied
# (CopyFromScreen: what is really on the monitor, not PrintWindow).
WATCH_PS1 = r"""
param([int]$ParentPid, [string]$Out, [string]$Stop, [string]$ShotDir)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SbWatch {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public static System.Collections.Generic.List<string> Scan(uint[] pids) {
    var res = new System.Collections.Generic.List<string>();
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (Array.IndexOf(pids, pid) < 0) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      string t = sb.ToString();
      if (!(t.StartsWith("Stable Boy - ") || t.StartsWith("Choose a variation"))) return true;
      RECT r; GetWindowRect(h, out r);
      res.Add(h.ToInt64() + "|" + pid + "|" + t + "|" + IsWindowVisible(h) + "|" + IsIconic(h) + "|" + r.L + "," + r.T + "," + r.R + "," + r.B);
      return true;
    }, IntPtr.Zero);
    return res;
  }
}
"@
[SbWatch]::SetProcessDPIAware() | Out-Null
$shots = @{}
while (-not (Test-Path $Stop)) {
  $pids = @([uint32]$ParentPid) + @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentPid" | ForEach-Object { [uint32]$_.ProcessId })
  foreach ($line in [SbWatch]::Scan([uint32[]]$pids)) {
    $p = $line -split "\|"
    $rec = [ordered]@{ t = (Get-Date).ToString("HH:mm:ss.fff"); hwnd = $p[0]; pid = $p[1]; title = $p[2]; visible = $p[3]; iconic = $p[4]; rect = $p[5] }
    ($rec | ConvertTo-Json -Compress) | Add-Content -Path $Out
    $key = "$($p[0])"
    if ($p[3] -eq "True" -and $p[2].StartsWith("Stable Boy - ") -and -not $shots.ContainsKey($key)) {
      $shots[$key] = 1
      Start-Sleep -Milliseconds 1500
      $c = $p[5] -split ","; $w = [int]$c[2] - [int]$c[0]; $h = [int]$c[3] - [int]$c[1]
      if ($w -gt 0 -and $h -gt 0) {
        $bmp = New-Object System.Drawing.Bitmap $w, $h
        $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen([int]$c[0], [int]$c[1], 0, 0, $bmp.Size); $g.Dispose()
        $name = Join-Path $ShotDir ("progress-window-screen-" + $key + ".png")
        $bmp.Save($name, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
      }
    }
  }
  Start-Sleep -Milliseconds 500
}
"""

# Brings this GIMP's image window to the front (verified by pid before any key
# is sent), optionally sends keys (Ctrl+Z = Edit > Undo) and copies the window
# from the screen.
KEYS_PS1 = r"""
param([int]$GimpPid, [string]$Keys = "", [string]$Shot = "")
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class SbKeys {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static int CloseWelcome(uint pid) {
    int n = 0;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      uint wp; GetWindowThreadProcessId(h, out wp);
      if (wp != pid) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      if (sb.ToString().StartsWith("Welcome")) { PostMessage(h, 0x10, IntPtr.Zero, IntPtr.Zero); n++; }
      return true;
    }, IntPtr.Zero);
    return n;
  }
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
[SbKeys]::SetProcessDPIAware() | Out-Null
if ([SbKeys]::CloseWelcome([uint32]$GimpPid) -gt 0) { "closed the Welcome dialog"; Start-Sleep -Milliseconds 800 }
$h = [SbKeys]::FindMain([uint32]$GimpPid)
if ($h -eq [IntPtr]::Zero) { "no GIMP window"; exit 2 }
[SbKeys]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [SbKeys]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[SbKeys]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 700
$fg = [SbKeys]::GetForegroundWindow(); [uint32]$fgpid = 0; [SbKeys]::GetWindowThreadProcessId($fg, [ref]$fgpid) | Out-Null
if ($fgpid -ne $GimpPid) { "foreground is pid $fgpid, not GIMP $GimpPid - keys not sent"; exit 3 }
$sb = New-Object System.Text.StringBuilder 512; [SbKeys]::GetWindowText($h, $sb, 512) | Out-Null
"window: $($sb.ToString())"
if ($Keys) {
  $ws = New-Object -ComObject WScript.Shell
  foreach ($k in ($Keys -split " ")) { $ws.SendKeys($k); Start-Sleep -Milliseconds 700 }
  "sent $Keys"; Start-Sleep -Milliseconds 1500
}
if ($Shot) {
  $r = New-Object SbKeys+RECT; [SbKeys]::GetWindowRect($h, [ref]$r) | Out-Null
  $bmp = New-Object System.Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T)
  $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size); $g.Dispose()
  $bmp.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose(); "saved $Shot"
}
"""

CONFIG_WATCH_PS1 = r"""
param([string]$Config, [int]$Seconds)
$end = (Get-Date).AddSeconds($Seconds)
$last = ""
while ((Get-Date) -lt $end) {
  try { $a = (Get-Content -Raw $Config | ConvertFrom-Json).active_model } catch { $a = "(unreadable)" }
  if ($a -ne $last) { "{0} active_model={1}" -f (Get-Date).ToString("HH:mm:ss.fff"), $a; $last = $a }
  Start-Sleep -Milliseconds 300
}
"""


def write(name, text):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def ps(script_name, args, wait=True, timeout=120):
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(OUT, script_name), *args]
    if not wait:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=FLAGS)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=FLAGS)
    return (res.stdout + res.stderr).strip()


write("capture-window.ps1", CAPTURE_PS1)
write("watch.ps1", WATCH_PS1)
write("keys.ps1", KEYS_PS1)
write("config-watch.ps1", CONFIG_WATCH_PS1)


def capture(title, out_png, delays_ms, timeout_s=900, screen=False):
    args = ["-Title", title, "-Out", out_png, "-ParentPid", str(GIMP_PID), "-DelaysMs", delays_ms]
    args += ["-TimeoutSec", str(timeout_s), "-Screen", "1" if screen else "0"]
    return ps("capture-window.ps1", args, wait=False)


def finish(proc, timeout=60):
    if proc is None:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = "capture timed out"
    return (out or "").strip()


def set_hooks(**hooks):
    with open(HOOKS, "w", encoding="utf-8") as fh:
        json.dump(hooks, fh)


def tree(img):
    out = []
    for layer in img.get_layers():
        entry = {"name": layer.get_name(), "visible": layer.get_visible()}
        if layer.is_group():
            entry["children"] = [{"name": c.get_name(), "visible": c.get_visible()} for c in layer.get_children()]
        out.append(entry)
    return out


def export_png(image, path):
    dup = image.duplicate()
    try:
        dup.flatten()
        longest = max(dup.get_width(), dup.get_height())
        if longest > 1024:
            dup.scale(round(dup.get_width() * 1024 / longest), round(dup.get_height() * 1024 / longest))
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, dup, Gio.File.new_for_path(path))
    finally:
        dup.delete()


def log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def log_slice(start):
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(start)
            return fh.read().decode("utf-8", errors="replace")
    except OSError as err:
        return f"(log unavailable: {err})"


def enable_undo(image):
    """Gimp.file_load from a plug-in leaves the image's undo disabled (File > Open
    enables it); enable it so Edit > Undo behaves as for an image the user opened."""
    while not image.undo_is_enabled():
        if not image.undo_enable():
            break


def state(image):
    return {
        "size": [image.get_width(), image.get_height()],
        "layers": tree(image),
        "undo_enabled": bool(image.undo_is_enabled()),
    }


def run_interactive(proc_name, image, props, hooks):
    set_hooks(**hooks)
    proc = Gimp.get_pdb().lookup_procedure(proc_name)
    cfg = proc.create_config()
    cfg.set_property("run-mode", Gimp.RunMode.INTERACTIVE)
    if image is not None:
        cfg.set_property("image", image)
        cfg.set_core_object_array("drawables", image.get_layers()[:1])
    for key, value in props.items():
        cfg.set_property(key, value)
    t0 = time.time()
    result = proc.run(cfg)
    return {"status": str(result.index(0)), "seconds": round(time.time() - t0, 1)}


def undo_procs():
    try:
        return list(Gimp.get_pdb().query_procedures(".*undo.*", "", "", "", "", "", "", ""))
    except Exception as err:  # noqa: BLE001
        return f"query failed: {err}"


def watch_start(task_dir):
    stop = os.path.join(task_dir, "watch.stop")
    if os.path.exists(stop):
        os.remove(stop)
    args = ["-ParentPid", str(GIMP_PID), "-Out", os.path.join(task_dir, "windows.jsonl"), "-Stop", stop]
    return ps("watch.ps1", [*args, "-ShotDir", task_dir], wait=False), stop


def watch_stop(watch):
    proc, stop = watch
    open(stop, "w").close()
    finish(proc, 30)
    os.remove(stop)


# Edit > Undo through the menu bar: Alt+E opens Edit, U is the mnemonic of its Undo item.
UNDO_KEYS = os.environ.get("SB_E2E_UNDO_KEYS", "%e u")


def window_shot(path, keys=""):
    return ps("keys.ps1", ["-GimpPid", str(GIMP_PID), "-Keys", keys, "-Shot", path])


def task_fill():
    d = os.path.join(OUT, "fill-4-variations")
    os.makedirs(d, exist_ok=True)
    rec = {}
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(os.path.join(PHOTOS, "landscape.jpg")))
    Gimp.Display.new(image)
    enable_undo(image)
    sel = [420, 90, 480, 300]  # open sky left of the barn
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *sel)
    Gimp.displays_flush()
    time.sleep(2)
    export_png(image, os.path.join(d, "before.png"))
    rec["before"] = state(image)
    rec["selection"] = sel
    rec["undo_procedures_in_pdb"] = undo_procs()
    rec["gimp_window_before"] = window_shot(os.path.join(d, "gimp-window-before.png"))
    watch = watch_start(d)
    cap_dialog = capture("Generative Fill", os.path.join(d, "fill-dialog.png"), "1000", 60)
    # one picker step per second: thumbnail 2 at 5 s, Keep selected at 10 s
    steps = ["-", "-", "-", "-", "2", "-", "-", "-", "-", "keep"]
    cap_picker = capture("Choose a variation - Generative Fill", os.path.join(d, "picker.png"), "2500,7500", 900)
    time.sleep(8)  # the capture helpers compile their user32 bindings first; the dialog closes 1.5 s after opening
    start = log_size()
    rec["run"] = run_interactive(
        "python-fu-stable-boy-generative-fill",
        image,
        {"prompt": "", "negative": "", "variations": 4, "seed": 1234, "model": MODEL},
        {
            "type": "hot air balloon",
            "press": "Return",
            "advanced": True,
            "values": {"variations": 4, "model": MODEL, "seed": 1234, "remove": False},
            "autoclose": 120,
            "picker_steps": steps,
        },
    )
    watch_stop(watch)
    rec["capture_dialog"] = finish(cap_dialog)
    rec["capture_picker"] = finish(cap_picker)
    rec["plugin_log"] = log_slice(start)
    Gimp.displays_flush()
    time.sleep(1)
    rec["after_pick"] = state(image)
    export_png(image, os.path.join(d, "after-pick-2.png"))
    rec["gimp_window_after_pick"] = window_shot(os.path.join(d, "gimp-window-after-pick-2.png"))
    rec["undo_keys"] = window_shot(os.path.join(d, "gimp-window-after-one-undo.png"), UNDO_KEYS)
    time.sleep(1)
    rec["after_one_undo"] = state(image)
    export_png(image, os.path.join(d, "after-one-undo.png"))
    report["tasks"]["fill"] = rec
    return image


def task_undo_probe():
    """One variation (no picker, no undo freeze), then Edit > Undo once."""
    d = os.path.join(OUT, "undo-probe")
    os.makedirs(d, exist_ok=True)
    rec = {}
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(os.path.join(PHOTOS, "landscape.jpg")))
    Gimp.Display.new(image)
    enable_undo(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 420, 90, 480, 300)
    Gimp.displays_flush()
    time.sleep(2)
    rec["before"] = state(image)
    start = log_size()
    rec["run"] = run_interactive(
        "python-fu-stable-boy-generative-fill",
        image,
        {},
        {
            "type": "a small bird",
            "press": "Return",
            "values": {"variations": 1, "model": "sd15-inpainting", "seed": 1234, "remove": False},
            "autoclose": 60,
        },
    )
    rec["plugin_log"] = log_slice(start)
    rec["after_fill"] = state(image)
    rec["undo_keys"] = window_shot(os.path.join(d, "gimp-window-edit-menu-after-undo.png"), UNDO_KEYS)
    time.sleep(1)
    rec["after_one_undo"] = state(image)
    report["tasks"]["undo-probe"] = rec
    image.delete()


def task_expand(image):
    d = os.path.join(OUT, "expand-discard")
    os.makedirs(d, exist_ok=True)
    rec = {}
    Gimp.Selection.none(image)
    Gimp.displays_flush()
    rec["before"] = state(image)
    export_png(image, os.path.join(d, "before.png"))
    watch = watch_start(d)
    cap_dialog = capture("Generative Expand", os.path.join(d, "expand-dialog.png"), "1000", 60)
    cap_picker = capture("Choose a variation - Generative Expand", os.path.join(d, "picker.png"), "2000", 900)
    time.sleep(8)
    start = log_size()
    rec["run"] = run_interactive(
        "python-fu-stable-boy-generative-expand",
        image,
        {
            "prompt": "",
            "negative": "",
            "left": 0,
            "top": 0,
            "right": 256,
            "bottom": 0,
            "variations": 2,
            "seed": 1234,
            "model": MODEL,
        },
        {
            "type": "meadow",
            "press": "Return",
            "advanced": True,
            "values": {"variations": 2, "model": MODEL, "seed": 1234, "left": 0, "top": 0, "right": 256, "bottom": 0},
            "autoclose": 120,
            "picker_steps": ["-", "-", "-", "discard"],
        },
    )
    watch_stop(watch)
    rec["capture_dialog"] = finish(cap_dialog)
    rec["capture_picker"] = finish(cap_picker)
    rec["plugin_log"] = log_slice(start)
    Gimp.displays_flush()
    time.sleep(1)
    rec["after_discard"] = state(image)
    rec["restored"] = rec["after_discard"] == rec["before"]
    export_png(image, os.path.join(d, "after-discard.png"))
    rec["gimp_window_after_discard"] = window_shot(os.path.join(d, "gimp-window-after-discard.png"))
    # one Edit > Undo after Discard: should bring the whole expand result back (canvas and layers)
    rec["undo_keys"] = window_shot(os.path.join(d, "gimp-window-after-undo-of-discard.png"), UNDO_KEYS)
    time.sleep(1)
    rec["after_undo_of_discard"] = state(image)
    report["tasks"]["expand"] = rec


def task_settings():
    d = os.path.join(OUT, "settings")
    os.makedirs(d, exist_ok=True)
    rec = {}
    dump = os.path.join(d, "settings-steps.jsonl")
    if os.path.exists(dump):
        os.remove(dump)
    with open(CONFIG, encoding="utf-8") as fh:
        rec["active_model_before"] = json.load(fh).get("active_model")
    steps = [
        [2, "dump", "open"],
        [3, "check", ""],
        [6, "dump", "check+3s"],
        [15, "dump", "check+12s"],
        [30, "dump", "check+27s"],
        [32, "tab", 1],
        [36, "dump", "models"],
        [38, "activate", "sd15-inpainting"],
        [41, "dump", "after-set-active-sd15-inpainting"],
        [43, "activate", "mat-places512"],
        [46, "dump", "after-set-active-mat-places512"],
    ]
    cfg_watch = ps("config-watch.ps1", ["-Config", CONFIG, "-Seconds", "60"], wait=False)
    cap = capture("Generative Fill Settings", os.path.join(d, "settings.png"), "1500,29000,37000,42000,47000", 60)
    rec["run"] = run_interactive(
        "python-fu-stable-boy-settings",
        None,
        {},
        {"autoclose": 50, "settings_steps": steps, "settings_out": dump},
    )
    rec["capture"] = finish(cap)
    rec["config_watch"] = finish(cfg_watch, 70)
    with open(CONFIG, encoding="utf-8") as fh:
        rec["active_model_after"] = json.load(fh).get("active_model")
    report["tasks"]["settings"] = rec


def task_remove():
    d = os.path.join(OUT, "remove")
    os.makedirs(d, exist_ok=True)
    rec = {}
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(os.path.join(PHOTOS, "product.jpg")))
    Gimp.Display.new(image)
    enable_undo(image)
    sel = [155, 335, 215, 185]  # the cup handle
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *sel)
    Gimp.displays_flush()
    time.sleep(2)
    rec["selection"] = sel
    with open(CONFIG, encoding="utf-8") as fh:
        rec["active_model"] = json.load(fh).get("active_model")
    rec["before"] = state(image)
    export_png(image, os.path.join(d, "before.png"))
    watch = watch_start(d)
    cap = capture("Generative Fill", os.path.join(d, "remove-dialog.png"), "1000", 60)
    time.sleep(8)
    start = log_size()
    rec["run"] = run_interactive(
        "python-fu-stable-boy-generative-fill",
        image,
        {"prompt": "", "negative": "", "variations": 1, "seed": 1234, "model": "active"},
        {"mode": "remove", "click": "generate", "autoclose": 30},
    )
    watch_stop(watch)
    rec["capture_dialog"] = finish(cap)
    rec["plugin_log"] = log_slice(start)
    Gimp.displays_flush()
    rec["after"] = state(image)
    export_png(image, os.path.join(d, "after.png"))
    rec["gimp_window_after"] = window_shot(os.path.join(d, "gimp-window-after.png"))
    report["tasks"]["remove"] = rec


try:
    img = None
    if "undo-probe" in TASKS:
        task_undo_probe()
    if "fill" in TASKS:
        img = task_fill()
    if "expand" in TASKS:
        if img is None:
            path = Gio.File.new_for_path(os.path.join(PHOTOS, "landscape.jpg"))
            img = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, path)
            Gimp.Display.new(img)
            enable_undo(img)
        task_expand(img)
    if "settings" in TASKS:
        task_settings()
    if "remove" in TASKS:
        task_remove()
    report["ok"] = True
except Exception:  # noqa: BLE001
    report["ok"] = False
    report["traceback"] = traceback.format_exc()
finally:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(OUT, "e2e-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    set_hooks()
    if os.environ.get("SB_TEST_QUIT", "1") == "1":
        q = Gimp.get_pdb().lookup_procedure("gimp-quit")
        cfg = q.create_config()
        cfg.set_property("force", True)
        q.run(cfg)
