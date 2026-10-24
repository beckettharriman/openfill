# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""GIMP-free text and state logic for the dialogs, the progress window and
the Models tab: prompt history, plain status texts, error next steps and the
state of the per-model primary button. No ``gi`` imports, so it is unit
tested with any Python 3.10+.
"""

from __future__ import annotations

import json
import re

HISTORY_LIMIT = 20
SUGGESTED_SHORTCUT = "Shift+Ctrl+G"

# Style for every text below: one short line, plain words, no tips or
# paragraphs. Longer explanations go into tooltips (also one line).

# ------------------------------------------------- generation dialogs ----

DIALOG_TITLES = {"fill": "Generative Fill", "expand": "Generative Expand", "txt2img": "Text to Image"}
MODE_FILL = "_Fill with a prompt"
MODE_REMOVE = "_Remove object"
FILL_PLACEHOLDER = "Describe what should appear in the selection"
EXPAND_PLACEHOLDER = "Leave empty to continue the scene"
TXT2IMG_PLACEHOLDER = "Describe the image"
REMOVE_EXPLANATION = "Erases the selection and continues the background. No prompt needed."
REMOVE_MODEL_NOTE = "LaMa removes it at full resolution, unless you pick another model."
RECENT_PROMPTS = "Recent prompts"
RECENT_EMPTY_TIP = f"Your last {HISTORY_LIMIT} prompts appear here."
KEY_HINT = "Enter generates, Shift+Enter adds a line"
SHORTCUT_HINT = f"Add a shortcut ({SUGGESTED_SHORTCUT}) in Edit > Keyboard Shortcuts"
GENERATE = "_Generate"
CANCEL = "_Cancel"
ADVANCED = "Advanced"

LABEL_AVOID = "Avoid"
AVOID_PLACEHOLDER = "What should not appear"
LABEL_MODEL = "Model"
LABEL_VARIATIONS = "Variations"
LABEL_CHANGE = "Change amount"
LABEL_SEED = "Seed"
SEED_RANDOM = "Random"
TIP_MODEL = "The first entry is the active model from Settings > Models."
TIP_VARIATIONS = "Each result is a layer; you pick one afterwards."
TIP_CHANGE = "1.0 replaces the selection; lower keeps more of it."
TIP_SEED = "The same number repeats a result."

EMPTY_PROMPT = "Type a prompt, or choose Remove object."
EXPAND_NOTHING = "Drag an edge outward to add space, or inward to crop."

LABEL_RATIO = "Ratio"
LABEL_ANCHOR = "Image at"
SPIN_TIP = "Pixels to add on this side. A negative value crops it."
LABEL_LANDSCAPE = "Landscape"
LABEL_PORTRAIT = "Portrait"
SIDE_LABELS = {"left": "Left", "top": "Top", "right": "Right", "bottom": "Bottom"}

LABEL_SIZE = "Size"
SIZE_AUTO = "Automatic (the selection, or the default size)"
LABEL_RESULT = "Result as"
RESULT_LAYER = "New layer"
RESULT_IMAGE = "New image"


def anchor_tip(anchor: str) -> str:
    """Tooltip of an anchor button: where the image stays in the new canvas."""
    if anchor == "center":
        return "Image in the centre"
    return f"Image at the {anchor.replace('-', ' ')}"


# ------------------------------------------------- picker and progress ----

PICKER_TITLE = "Choose a variation - {title}"
PICKER_HINT = "Keep selected hides the others in the layer group. Discard removes all."
VARIATION_LABEL = "Variation {number}"

PROGRESS_TITLE = "OpenFill - {title}"
PROGRESS_READY = "Getting ready..."
PROGRESS_PREVIEW_HINT = "The preview appears here."
PROGRESS_CANCEL_TIP = "Stop now. The image stays unchanged."
PROGRESS_CANCELLING = "Cancelling. The image stays unchanged..."
PROGRESS_CANCELLED = "Cancelled"
PROGRESS_FAILED = "Something went wrong"
PROGRESS_DONE = "Done"
PROGRESS_DOWNLOAD = "Download: {row}"
PROGRESS_QUEUED = "Queued: {name}"


def picker_heading(count: int) -> str:
    """First line of the variations picker."""
    return f"{count} variations ready. Click one to see it in the image."


# ------------------------------------------------------------ settings ----

SETTINGS_TITLE = "Generative Fill Settings"
SETTINGS_GENERAL = "General"
SETTINGS_MODELS = "Models"
SETTINGS_ADVANCED = "Advanced generation"
SETTINGS_CONNECTION = "Connection and troubleshooting"
CHOOSE_FOLDER = "Choose the ComfyUI folder"
BROWSE = "Browse..."
CHECK_BACKEND = "Check backend"
OPEN_LOG_FOLDER = "Open log folder"
OPEN_LOG_FOLDER_TIP = "openfill.log and comfyui.log, the hidden ComfyUI console (device, errors)."
CHECK_BACKEND_TIP = "Starts the backend if needed and checks that it answers."
CHECK_BACKEND_IDLE = "Press Check backend to test the AI backend. The first start takes about a minute."
CHECK_BACKEND_RUNNING = "Checking. The first start takes about a minute..."

# (key, label, one-line hint)
GENERAL_FIELDS = (
    ("comfy_dir", "AI backend folder", "Where ComfyUI (portable build) is installed."),
    ("autostart", "Start backend automatically", "Starts ComfyUI hidden when you generate."),
    (
        "idle_timeout_minutes",
        "Idle timeout (minutes)",
        "Stops the backend after this many idle minutes to free graphics memory. 0 keeps it running.",
    ),
    ("progress_window", "Show the floating progress window", "Progress, a live preview and Cancel."),
)
ADVANCED_FIELDS = (
    ("steps", "Steps", "More steps add detail but take longer. 20 to 30 is typical."),
    ("cfg", "Prompt strictness (CFG)", "How closely the result follows the prompt. 5 to 8 is typical."),
    ("sampler", "Sampler", "Keep the default unless a model page says otherwise."),
    ("scheduler", "Scheduler", '"karras" suits most models.'),
    ("grow_percent", "Repaint margin (% of selection)", "Repaints a little around the selection so edges blend."),
    ("grow", "Repaint margin limit (px)", "Upper limit for that margin."),
    ("feather_percent", "Edge softness (% of selection)", "How softly the new layer blends in."),
    ("feather", "Edge softness limit (px)", "Upper limit for the soft edge."),
    ("prefill", "Hide old content before generating", "Helps replace an object instead of copying it."),
    ("max_side_sd15", "Working size for SD 1.5 (px)", "Larger is sharper but slower and needs more memory."),
    ("max_side_sdxl", "Working size for SDXL (px)", "Larger is sharper but slower and needs more memory."),
    ("txt2img_width", "Text to Image default width (px)", ""),
    ("txt2img_height", "Text to Image default height (px)", ""),
)
CONNECTION_FIELDS = (
    ("backend", "Backend", "ComfyUI is managed for you. AUTOMATIC1111 must be started by you with --api."),
    ("url", "ComfyUI address", ""),
    ("a1111_url", "AUTOMATIC1111 address", ""),
    ("startup_timeout", "Startup time limit (s)", "How long to wait for the backend to start."),
    ("request_timeout", "Generation time limit (s)", "0 estimates a limit for each request."),
    ("extra_server_args", "Extra ComfyUI options", "For example --lowvram on cards with little memory."),
    ("quality", "Model for scripts asking 'fast' or 'quality'", "Only used by older scripts."),
    ("checkpoint", "SD 1.5 model file", "Used by the 'fast' choice of older scripts."),
    ("sdxl_checkpoint", "SDXL model file", "Used by the 'quality' choice of older scripts."),
    ("fooocus_patch", "Fooocus patch file", "Used by the 'quality' choice of older scripts."),
    ("debug", "Verbose log", "Writes more detail to openfill.log for bug reports."),
)


# ------------------------------------------------------------ history ----


def parse_history(text: str | None) -> list[str]:
    """Read the stored history (a JSON list of strings); tolerate junk."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str) and item.strip()][:HISTORY_LIMIT]


def dump_history(history: list[str]) -> str:
    """JSON for ``Settings.prompt_history`` (at most HISTORY_LIMIT prompts)."""
    return json.dumps(list(history)[:HISTORY_LIMIT], ensure_ascii=False)


def remember_prompt(history: list[str], prompt: str, limit: int = HISTORY_LIMIT) -> list[str]:
    """Newest first, no duplicates (ignoring surrounding white space), at most ``limit``."""
    prompt = (prompt or "").strip()
    out = [p for p in history if p.strip() and p.strip() != prompt]
    if prompt:
        out.insert(0, prompt)
    return out[:limit]


def history_label(prompt: str, width: int = 60) -> str:
    """One line for a menu entry: white space collapsed, long prompts shortened."""
    text = " ".join((prompt or "").split())
    if len(text) <= width:
        return text
    return text[: max(1, width - 3)].rstrip() + "..."


def initial_prompt(config_prompt: str | None, history: list[str]) -> str:
    """The prompt a dialog starts with: the procedure's last value, else the newest history entry."""
    if config_prompt and config_prompt.strip():
        return config_prompt
    return history[0] if history else ""


# ------------------------------------------------------------ status -----

_STEP = re.compile(r"step\s+(\d+)\s*/\s*(\d+)", re.I)
_START = re.compile(r"Starting the AI backend\.\.\.\s*(\d+)\s*s", re.I)


_PART = re.compile(r"\s*\(part \d+ of \d+\)$")


def plain_step_text(text: str) -> str:
    """Translate backend progress texts into plain sentences for the progress window.
    A trailing "(part 2 of 3)" (several expand rounds in one window) is kept."""
    raw = (text or "").strip()
    part = _PART.search(raw)
    if part:
        return plain_step_text(raw[: part.start()]) + " " + part.group(0).strip()
    return _plain_step_text(raw)


def _plain_step_text(raw: str) -> str:
    if not raw:
        return raw
    m = _STEP.search(raw)
    if m:
        return f"Generating, step {m.group(1)} of {m.group(2)}"
    m = _START.search(raw)
    if m:
        return f"Starting the AI backend, {m.group(1)} s"
    lowered = raw.lower()
    if lowered.startswith("starting the ai backend") or lowered.startswith("starting comfyui"):
        return "Starting the AI backend"
    if lowered.startswith("waiting for the ai backend"):
        return "Waiting for the AI backend"
    if lowered.startswith("uploading"):
        return "Sending the selection to the AI backend"
    if lowered.startswith("submitting"):
        return "Preparing the generation"
    if lowered.startswith("running node"):
        return "Loading the model"
    if lowered.startswith("downloading result") or lowered == "done":
        return "Placing the result"
    return raw


def elapsed_text(elapsed: str, remaining: str | None = None) -> str:
    """Time line of the progress window."""
    if remaining and remaining.strip("-: "):
        return f"Elapsed {elapsed}, about {remaining} left"
    return f"Elapsed {elapsed}"


# FLUX Fill paints what the prompt names and does not follow instructions about the picture.
FLUX_PROMPT_HINT = "FLUX paints everything you name: describe only what goes in the selection."


def prompt_free_warning(model_name: str) -> str:
    """Fill dialog note when the active model ignores prompts (removal models)."""
    short = (model_name or "The active model").split(" - ")[0]
    return f"{short} only removes objects and ignores the prompt. Pick another model to fill."


def selection_status(selection: tuple[int, int] | None, image: tuple[int, int] | None) -> str:
    """Status line of the Generative Fill dialog (shown as a warning without a selection)."""
    if image is None:
        return ""
    if selection is None:
        return f"No selection: the whole image ({image[0]} x {image[1]} px) will be regenerated."
    return f"Selection: {selection[0]} x {selection[1]} px"


EXPAND_SOFT_LIMIT = 0.25
EXPAND_OVER_LIMIT = "Over 25% per side: results get weaker. Expand in rounds for best results."


def expand_over_limit(
    image: tuple[int, int] | None, left: int, top: int, right: int, bottom: int, limit: float = EXPAND_SOFT_LIMIT
) -> bool:
    """True when any side grows by more than ``limit`` of the image size along
    that axis (left/right against the width, top/bottom against the height)."""
    if image is None:
        return False
    width, height = image
    return max(left, right) > limit * width or max(top, bottom) > limit * height


def expand_model_note(better: str) -> str:
    """Question shown before an Expand with a model other than the Expand default."""
    return f"{better} usually continues a scene better. Use it instead?"


def expand_status(image: tuple[int, int] | None, left: int, top: int, right: int, bottom: int) -> str:
    """Readout under the Generative Expand editor, e.g.
    "New size: 2432 x 1440 px (+256 right)". Negative margins crop:
    "New size: 900 x 768 px (-62 left, -62 right cropped)"."""
    if image is None:
        return ""
    sides = {"left": left, "top": top, "right": right, "bottom": bottom}
    width, height = image[0] + left + right, image[1] + top + bottom
    added = [(name, value) for name, value in sides.items() if value > 0]
    cut = [(name, -value) for name, value in sides.items() if value < 0]
    if not added and not cut:
        return f"New size: {width} x {height} px. {EXPAND_NOTHING}"
    if len(added) == 4 and len(set(sides.values())) == 1:
        parts = [f"+{added[0][1]} each side"]
    else:
        parts = [f"+{value} {name}" for name, value in added]
    if cut:
        parts.append(", ".join(f"-{value} {name}" for name, value in cut) + " cropped")
    return f"New size: {width} x {height} px ({', '.join(parts)})"


def txt2img_status(
    width: int, height: int, selection: tuple[int, int] | None, default: tuple[int, int], target: str
) -> str:
    """Status line of the Text to Image dialog."""
    if width > 0 and height > 0:
        size = f"{width} x {height} px"
    elif selection is not None and target == "layer":
        size = f"{selection[0]} x {selection[1]} px (the selection)"
    else:
        size = f"{default[0]} x {default[1]} px (the default size)"
    where = "a new image" if target == "image" else "a new layer"
    return f"Result: {size}, as {where}."


# ------------------------------------------------------------ windows ----


def clamp_position(
    x: int, y: int, width: int, height: int, areas: list[tuple[int, int, int, int]], margin: int = 8
) -> tuple[int, int]:
    """Keep a window of ``width`` x ``height`` at (x, y) fully inside one of
    the monitor work ``areas`` (x, y, w, h). A position on no monitor (for
    example remembered from a monitor that is gone) moves to the nearest
    area; a window larger than the area sticks to its top left corner."""
    if not areas:
        return x, y

    def distance(area: tuple[int, int, int, int]) -> int:
        ax, ay, aw, ah = area
        dx = max(ax - x, 0, x - (ax + aw))
        dy = max(ay - y, 0, y - (ay + ah))
        return dx * dx + dy * dy

    ax, ay, aw, ah = min(areas, key=distance)
    nx = min(max(x, ax + margin), ax + aw - width - margin)
    ny = min(max(y, ay + margin), ay + ah - height - margin)
    return max(nx, ax), max(ny, ay)


# ------------------------------------------------------------ errors -----

_NEXT_STEPS = (
    # Refusals that already say what to do: trying again or a bug report would not help.
    ("nothing is selected", ""),
    ("uses indexed colors", ""),
    (
        "was not found at",
        "Next step: open Filters > OpenFill > Generative Fill Settings and choose the backend folder with Browse.",
    ),
    (
        "auto-start is disabled",
        'Next step: turn on "Start backend automatically" in Filters > OpenFill > '
        "Generative Fill Settings, or start ComfyUI yourself.",
    ),
    (
        "did not come up",
        "Next step: try again (the first start can take a few minutes). If it keeps "
        "failing, read comfyui.log in the openfill folder of your GIMP profile.",
    ),
    ("is not answering", ""),  # the A1111 message already says what to do
    ("stopped while it was generating", ""),  # backends/comfy.py CONNECTION_LOST says what to do
    ("ran out of memory", ""),  # describe_error already says what to do
    ("press download again", ""),
    ("only allows this download", ""),  # a download message that already says what to do
    (
        "timed out",
        'Next step: try a smaller selection or fewer variations, or raise "Generation time limit" '
        "in Settings > Connection and troubleshooting.",
    ),
    ("was cancelled", "Nothing in the image was changed."),
    ("rejected", 'Next step: pick another model under Advanced, or turn on "Verbose log" in Settings and try again.'),
    ("press download", ""),
    ("download it from the models tab", ""),
    ("press install nodes", ""),
    (
        "model file is missing",
        "Next step: open Filters > OpenFill > Generative Fill Settings > Models and download or choose a model.",
    ),
    ("not available", "Next step: open Filters > OpenFill > Generative Fill Settings and press Check backend."),
)


def unverified_download_question(name: str) -> str:
    """The question shown before downloading a model whose publisher lists no SHA-256 checksum."""
    return (
        f"{name} has no published checksum, so the plug-in cannot check that the file is the one the "
        "publisher uploaded. It is downloaded as it arrives and its checksum is recorded afterwards, so "
        "later downloads are checked. Only continue if you trust the publisher."
    )


INTERRUPTED_OUTSIDE = (
    "The generation was stopped in ComfyUI (its Cancel or Interrupt was used outside GIMP). "
    "Nothing in the image was changed. Run the command again to try once more."
)


def next_step(message: str) -> str:
    """The "Next step: ..." sentence that fits an error ``message``."""
    lowered = (message or "").lower()
    for needle, step in _NEXT_STEPS:
        if needle in lowered:
            return step
    return (
        'Next step: try again. If it happens again, turn on "Verbose log" in Filters > OpenFill > '
        "Generative Fill Settings and keep openfill.log for a bug report."
    )


def with_next_step(message: str) -> str:
    """``message`` followed by a plain next step (unless it already has one)."""
    message = (message or "").strip()
    if "next step" in message.lower():
        return message
    if "generative fill settings" in message.lower():  # already points at the setting to change
        return message
    step = next_step(message)
    if not step:
        return message
    sep = "" if message.endswith((".", "!", "?")) else "."
    return f"{message}{sep} {step}" if message else step


def save_failed(path: str, detail: str) -> str:
    """Wording when the settings file cannot be written."""
    return (
        f"The settings could not be saved to {path}: {detail}. Next step: check that the folder is "
        "writable and the disk is not full, then open Filters > OpenFill > Generative Fill Settings and "
        "press Save again."
    )


def unexpected_error(type_name: str, detail: str) -> str:
    """Wording for an error that is not a backend error (a bug or a GIMP failure)."""
    return with_next_step(f"Something went wrong: {type_name}: {detail}")


def health_text(
    ok: bool, detail: str, model_count: int, inpaint_nodes: bool, device: str = "", device_kind: str = ""
) -> str:
    """Result line of the Settings > General "Check backend" button."""
    if ok:
        models = f"{model_count} model{'s' if model_count != 1 else ''}"
        nodes = "inpaint nodes installed" if inpaint_nodes else "inpaint nodes missing (SDXL quality fill needs them)"
        text = f"Backend OK: {models}, {nodes}."
        if device:
            text += f" Running on: {device}."
        if device_kind == "cpu":
            text += (
                " That is the processor, not the graphics card: every generation will be very slow. "
                "Remove --cpu from Extra ComfyUI options, or use a ComfyUI whose PyTorch supports your card."
            )
        return text
    reason = " ".join((detail or "no answer").split())
    if len(reason) > 220:
        reason = reason[:217].rstrip() + "..."
    step = next_step(reason)
    if not step or step.startswith("Next step: try again. If it happens"):
        step = (
            'Next step: check the backend folder above, keep "Start backend automatically" on, '
            "and press Check backend again."
        )
    return f"Could not reach the backend: {reason.rstrip('.')}. {step}"


# ------------------------------------------------------------ models -----


def primary_action(
    row: dict, current: dict | None, pending_ids: list[str] | None = None, has_token: bool = True
) -> tuple[str, str | None, bool]:
    """(label, action, sensitive) of the single primary button of a model row.

    Actions: "download", "cancel", "activate", "nodes", "token" (ask for a
    token first) or None (informational state).
    """
    current = current or {}
    pending_ids = pending_ids or []
    mid = row.get("id")
    if current.get("model_id") == mid:
        kind = current.get("type")
        if kind == "download":
            total = current.get("total") or 0
            pct = 100.0 * (current.get("done") or 0) / total if total else 0.0
            return f"Downloading {pct:.0f}% - Cancel", "cancel", True
        if kind == "install_nodes":
            return "Installing nodes...", None, False
        if kind == "remove":
            return "Removing...", None, False
        return "Working...", None, False
    if mid in pending_ids:
        return "Queued", None, False
    if not row.get("installed"):
        if row.get("gated") and not has_token:
            return "Download", "token", True
        return ("Resume download" if row.get("partial") else "Download"), "download", True
    if row.get("missing_models"):
        return "Download missing part", "download", True
    if row.get("missing_nodes"):
        return "Install nodes", "nodes", not bool(current)
    if not row.get("runnable"):
        return "Downloaded", None, False
    if row.get("active"):
        return "Active", None, False
    return "Set active", "activate", True


def model_sort_key(row: dict) -> tuple:
    """Active first, then installed, then the rest; stable by catalog order otherwise."""
    return (0 if row.get("active") else 1 if row.get("installed") else 2,)


def model_summary(row: dict) -> str:
    """Second line of a model row, in plain words."""
    caps = row.get("capabilities") or []
    names = {"fill": "fill", "expand": "expand", "txt2img": "text to image", "remove": "object removal", "edit": "edit"}
    uses = [names[c] for c in caps if c in names]
    if not row.get("runnable") or "patch" in caps:
        uses = ["helper file used by another model"]
    pieces = []
    if uses:
        text = ", ".join(uses)
        pieces.append(text[0].upper() + text[1:])
    if row.get("size"):
        pieces.append(str(row["size"]))
    vram = row.get("min_vram_gb")
    if vram:
        pieces.append(f"Needs {vram:g} GB of graphics memory")
    if row.get("gated"):
        pieces.append("Needs a free Hugging Face token")
    return ". ".join(pieces) + ("." if pieces else "")


def model_option_label(name: str, installed: bool, active: bool) -> str:
    """Label of an entry in the Model dropdown of the generation dialogs."""
    name = (name or "").split(" - ")[0].strip() or name
    if active:
        return f"{name} (active model)"
    if not installed:
        return f"{name} (not downloaded)"
    return name


# ComfyUI options that force the processor.
CPU_FLAGS = ("--cpu",)


def cpu_backend_message(flags: str) -> str:
    """What to tell the user when ComfyUI computes on the processor."""
    why = (
        f"because of '{flags}' in Generative Fill Settings > Extra ComfyUI options; remove it if you have a "
        "supported graphics card"
        if flags
        else "because its PyTorch cannot use your graphics card (an AMD or Intel card needs ComfyUI's own "
        "instructions for that card; the Windows download is for NVIDIA cards)"
    )
    return (
        f"ComfyUI is running on the processor, not the graphics card, {why}. Every generation will be many "
        "times slower, and the larger models may not finish. The device ComfyUI uses is in openfill.log."
    )


def unsupported_gpu_message(detail: str) -> str:
    """What to tell the user when ComfyUI's PyTorch has no code for the graphics card."""
    return (
        "ComfyUI cannot use this graphics card: its PyTorch was made for newer cards, so every generation "
        "would fail. NVIDIA cards older than the RTX 20 series (GTX 10 and earlier) need ComfyUI's build for "
        "older cards, ComfyUI_windows_portable_nvidia_cu126.7z on the ComfyUI releases page. Unpack it and "
        f"choose its folder in Filters > OpenFill > Generative Fill Settings. (PyTorch said: {detail})"
    )
