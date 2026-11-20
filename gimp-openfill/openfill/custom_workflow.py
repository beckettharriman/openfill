# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""Custom ComfyUI workflows (Advanced): the user's own graph in ComfyUI's API format,
run with OpenFill's selection, mask, prompt and seed.

The file is only ever read as JSON and checked (:func:`parse`): node ids, class names
and input values must be plain JSON values or links between the graph's own nodes.
Nothing in it is executed here; the graph is sent to the user's own ComfyUI like
OpenFill's built-in graphs. Where the picture, mask, prompt and seed go is found in the
graph (:func:`detect`) or given by hand (:func:`apply_inputs`, ``image=10, mask=11,
prompt=6.text, negative=7.text, seed=3.seed, output=9``).

What OpenFill sends (see ``App.generative_fill``):

- the picture: the context crop as RGBA, its alpha 0 where the selection is, so a
  LoadImage's MASK output is the selection (ComfyUI's own convention for a painted mask);
- the mask, when the graph has a node for it: gray where selected, also as alpha, so it
  works with LoadImage (IMAGE or MASK output) and LoadImageMask (any channel).

Both are resized to OpenFill's working size inside the graph, and the result is
resized back to the crop's size, so any workflow gets a sensible resolution and its
result lands on the selection.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_NODES = 2000
MAX_TEXT = 200_000
MAX_DEPTH = 6
_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")

IMAGE_LOADERS = ("LoadImage",)
MASK_LOADERS = ("LoadImageMask",)
OUTPUT_CLASSES = ("SaveImage", "PreviewImage")
TEXT_FIELDS = ("text", "prompt", "text_g", "text_l", "positive_prompt", "string", "value")
SEED_FIELDS = ("seed", "noise_seed")
# Inputs that carry conditioning on to a sampler (followed back to the text node).
CONDITIONING_INPUTS = ("conditioning", "positive", "negative", "conditioning_1", "conditioning_to", "cond")
MAPPING_KEYS = ("image", "mask", "prompt", "negative", "seed", "output")

PREFIX = "openfill_"
OUTPUT_NODE = PREFIX + "save"

UI_FORMAT = (
    "This is a ComfyUI workflow in the editor's own format, not the API format. In ComfyUI, "
    "open it and use Workflow > Export (API), then choose that file."
)


class CustomWorkflowError(ValueError):
    """The workflow file cannot be used; the text says why and what to do."""


@dataclass(frozen=True)
class Mapping:
    """Where OpenFill's inputs go in the graph. Fields are (node id, input name) pairs."""

    image: str
    mask: str = ""  # node of a separate mask loader; "" = the selection is the picture's alpha
    prompt: tuple[tuple[str, str], ...] = ()
    negative: tuple[tuple[str, str], ...] = ()
    seed: tuple[tuple[str, str], ...] = ()
    output: str = ""  # the user's output node (SaveImage / PreviewImage) whose image is taken

    def describe(self, graph: dict) -> str:
        """One line for the settings: what was found where."""

        def node(nid: str) -> str:
            return f"{nid} ({graph.get(nid, {}).get('class_type', '?')})"

        def fields(pairs: tuple[tuple[str, str], ...]) -> str:
            return ", ".join(f"{n}.{f}" for n, f in pairs) if pairs else "none"

        parts = [
            f"image {node(self.image)}",
            f"mask {node(self.mask)}" if self.mask else "mask in the image's alpha",
            f"prompt {fields(self.prompt)}",
            f"negative {fields(self.negative)}",
            f"seed {fields(self.seed)}",
            f"output {node(self.output)}",
        ]
        return "; ".join(parts)


@dataclass(frozen=True)
class CustomWorkflow:
    """A checked workflow with the places of OpenFill's inputs."""

    name: str
    graph: dict
    mapping: Mapping
    source: str = ""  # the file it came from
    notes: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        return self.mapping.describe(self.graph)


# ----------------------------------------------------------------- checking ----


def _is_link(value: object, graph: dict) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _check_value(value: object, where: str, graph: dict, depth: int = 0) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 2**64:
            raise CustomWorkflowError(f"{where}: the number is too large.")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CustomWorkflowError(f"{where}: the number is not finite.")
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise CustomWorkflowError(f"{where}: the text is too long.")
        return
    if isinstance(value, list) and depth == 0:
        # In the API format a list is always a link: [source node id, output index].
        if not _is_link(value, graph):
            raise CustomWorkflowError(f"{where}: a list must be a link [node id, output number].")
        if value[0] not in graph:
            raise CustomWorkflowError(f"{where}: links to node {value[0]!r}, which is not in the workflow.")
        if not 0 <= value[1] <= 1000:
            raise CustomWorkflowError(f"{where}: bad output number {value[1]}.")
        return
    if depth >= MAX_DEPTH:
        raise CustomWorkflowError(f"{where}: the value is nested too deeply.")
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_value(item, f"{where}[{i}]", graph, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise CustomWorkflowError(f"{where}: a key is not plain text.")
            _check_value(item, f"{where}.{key}", graph, depth + 1)
        return
    raise CustomWorkflowError(f"{where}: unsupported value.")


def parse(data: bytes | str) -> dict:
    """Decode and check an API-format workflow; returns the graph as node id -> {class_type,
    inputs[, _meta]} (other keys dropped). Raises :class:`CustomWorkflowError`."""
    if isinstance(data, bytes):
        if len(data) > MAX_FILE_BYTES:
            raise CustomWorkflowError("The workflow file is too large (more than 8 MB).")
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CustomWorkflowError("The workflow file is not text (UTF-8 JSON).") from None
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError) as err:
        raise CustomWorkflowError(f"The workflow file is not valid JSON ({err}).") from None
    if isinstance(raw, dict) and isinstance(raw.get("prompt"), dict) and "class_type" not in raw["prompt"]:
        raw = raw["prompt"]  # a saved /prompt request body
    if isinstance(raw, dict) and isinstance(raw.get("nodes"), list):
        raise CustomWorkflowError(UI_FORMAT)
    if not isinstance(raw, dict) or not raw:
        raise CustomWorkflowError("The workflow file holds no nodes. Use Workflow > Export (API) in ComfyUI.")
    if len(raw) > MAX_NODES:
        raise CustomWorkflowError(f"The workflow has more than {MAX_NODES} nodes.")
    graph: dict[str, dict] = {}
    for node_id, node in raw.items():
        if not isinstance(node_id, str) or not _ID_RE.match(node_id):
            raise CustomWorkflowError(f"Bad node id {str(node_id)[:40]!r}.")
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise CustomWorkflowError(f"Node {node_id} has no class_type. Use Workflow > Export (API) in ComfyUI.")
        if not _NAME_RE.match(node["class_type"]):
            raise CustomWorkflowError(f"Node {node_id} has a bad class_type.")
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            raise CustomWorkflowError(f"Node {node_id}: inputs must be an object.")
        out = {"class_type": node["class_type"], "inputs": inputs}
        meta = node.get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get("title"), str):
            out["_meta"] = {"title": meta["title"][:200]}
        graph[node_id] = out
    for node_id, node in graph.items():
        for name, value in node["inputs"].items():
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise CustomWorkflowError(f"Node {node_id} has a bad input name.")
            _check_value(value, f"Node {node_id} ({node['class_type']}) input {name}", graph)
    return json.loads(json.dumps(graph))  # a plain copy


# ----------------------------------------------------------------- finding ----


def _order(node_id: str) -> tuple:
    """Sort key: numeric ids in number order, then the others."""
    return (0, int(node_id), "") if node_id.isdigit() else (1, 0, node_id)


def _title(node: dict) -> str:
    return str((node.get("_meta") or {}).get("title") or "").lower()


def _nodes_of(graph: dict, classes: tuple[str, ...]) -> list[str]:
    return sorted((nid for nid, n in graph.items() if n["class_type"] in classes), key=_order)


def _text_field(graph: dict, node_id: str, depth: int = 0) -> tuple[str, str] | None:
    """The (node, input) holding the text of a text encoder; a text that comes from a
    link (a String primitive node) is followed to that node."""
    node = graph.get(node_id)
    if node is None or depth > 3:
        return None
    for name in TEXT_FIELDS:
        value = node["inputs"].get(name)
        if isinstance(value, str):
            return node_id, name
        if _is_link(value, graph):
            found = _text_field(graph, value[0], depth + 1)
            if found:
                return found
    return None


def _trace_text(graph: dict, start: str) -> tuple[str, str] | None:
    """Follow conditioning links back from ``start`` to the first text field."""
    seen: set[str] = set()
    queue = [start]
    while queue and len(seen) < 64:
        node_id = queue.pop(0)
        if node_id in seen or node_id not in graph:
            continue
        seen.add(node_id)
        found = _text_field(graph, node_id)
        if found:
            return found
        for name, value in graph[node_id]["inputs"].items():
            if _is_link(value, graph) and any(key in name for key in CONDITIONING_INPUTS):
                queue.append(value[0])
    return None


def _sampler_texts(graph: dict, which: str) -> list[tuple[str, str]]:
    """Text fields that reach a sampler's or guider's ``which`` (positive / negative) input."""
    out: list[tuple[str, str]] = []
    for node_id in sorted(graph, key=_order):
        value = graph[node_id]["inputs"].get(which)
        if _is_link(value, graph):
            found = _trace_text(graph, value[0])
            if found and found not in out:
                out.append(found)
    return out


# Nodes a mask picture may pass on its way to ImageToMask (see _feeds_image_to_mask).
_PASS_THROUGH = ("ImageScale", "ImageScaleBy", "ImageScaleToTotalPixels", "ImageInvert", "ImageBlur")


def _feeds_image_to_mask(graph: dict, node_id: str, index: int = 0, depth: int = 0) -> bool:
    """True when output ``index`` of ``node_id`` reaches an ImageToMask (through resizes):
    that loader's picture is used as a mask."""
    if depth > 3:
        return False
    for nid, node in graph.items():
        for value in node["inputs"].values():
            if not (_is_link(value, graph) and value[0] == node_id and value[1] == index):
                continue
            if node["class_type"] == "ImageToMask":
                return True
            if node["class_type"] in _PASS_THROUGH and _feeds_image_to_mask(graph, nid, 0, depth + 1):
                return True
    return False


def detect(graph: dict) -> Mapping:
    """Find where OpenFill's inputs go. Raises :class:`CustomWorkflowError` when the
    workflow has no image loader or no output.

    The mask loader is a LoadImageMask, else (with two or more Load Image nodes) the one
    named or titled "mask", else the one whose picture goes into an ImageToMask. Without
    one, the selection reaches the workflow as the picture's alpha (LoadImage's MASK)."""
    loaders = _nodes_of(graph, IMAGE_LOADERS)
    mask_loaders = _nodes_of(graph, MASK_LOADERS)
    mask = mask_loaders[0] if mask_loaders else ""
    if not mask and len(loaders) > 1:
        named = [nid for nid in loaders if "mask" in _title(graph[nid]) or "mask" in nid.lower()]
        used = [nid for nid in loaders if _feeds_image_to_mask(graph, nid)]
        mask = (named or used or [""])[0]
    images = [nid for nid in loaders if nid != mask]
    if not images:
        raise CustomWorkflowError(
            "The workflow has no Load Image node for the picture. Add one where the picture goes in, "
            "export it again with Workflow > Export (API), and choose that file."
        )
    image = images[0]
    prompt = _sampler_texts(graph, "positive")
    negative = [t for t in _sampler_texts(graph, "negative") if t not in prompt]
    if not prompt:
        encoders = [nid for nid in sorted(graph, key=_order) if graph[nid]["class_type"].startswith("CLIPTextEncode")]
        texts = [t for t in (_text_field(graph, nid) for nid in encoders) if t]
        prompt = [t for t in texts if "neg" not in _title(graph[t[0]])][:1]
    seed = [
        (nid, name)
        for nid in sorted(graph, key=_order)
        for name in SEED_FIELDS
        if isinstance(graph[nid]["inputs"].get(name), int) and not isinstance(graph[nid]["inputs"].get(name), bool)
    ]
    outputs = [nid for nid in _nodes_of(graph, OUTPUT_CLASSES) if _is_link(graph[nid]["inputs"].get("images"), graph)]
    saves = [nid for nid in outputs if graph[nid]["class_type"] == "SaveImage"]
    if not outputs:
        raise CustomWorkflowError(
            "The workflow has no Save Image or Preview Image node, so there is no result to take. "
            "Add one at the end, export it again with Workflow > Export (API), and choose that file."
        )
    return Mapping(
        image=image,
        mask=mask,
        prompt=tuple(prompt),
        negative=tuple(negative),
        seed=tuple(seed),
        output=(saves or outputs)[0],
    )


def _pairs(graph: dict, key: str, text: str) -> tuple[tuple[str, str], ...]:
    """``6.text`` or ``6`` (the field found in the node) -> ((node, field),)."""
    node_id, _, name = text.partition(".")
    if node_id not in graph:
        raise CustomWorkflowError(f"{key}={text}: the workflow has no node {node_id}.")
    inputs = graph[node_id]["inputs"]
    if not name:
        if key == "seed":
            name = next((f for f in SEED_FIELDS if f in inputs), "")
        else:
            found = _text_field(graph, node_id)
            if found and found[0] == node_id:
                name = found[1]
    if not name or name not in inputs:
        raise CustomWorkflowError(f"{key}={text}: node {node_id} has no such input; write it as {node_id}.<input>.")
    if _is_link(inputs[name], graph):
        raise CustomWorkflowError(f"{key}={text}: that input is connected to another node; name that node instead.")
    return ((node_id, name),)


def apply_inputs(graph: dict, mapping: Mapping, text: str) -> Mapping:
    """Overrides typed by the user, e.g. ``image=10, mask=11, prompt=6.text, seed=3.seed``.
    ``mask=alpha`` puts the selection in the picture's alpha; ``prompt=none`` (also
    ``negative``, ``seed``) leaves that input as it is in the workflow."""
    changes: dict[str, object] = {}
    for part in re.split(r"[,;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        key, sep, value = (s.strip() for s in part.partition("="))
        key = key.lower()
        if not sep or key not in MAPPING_KEYS or not value:
            raise CustomWorkflowError(
                f"Can't read {part!r}. Write it as name=node, for example image=10, mask=11, prompt=6.text, "
                "negative=7.text, seed=3.seed, output=9."
            )
        lowered = value.lower()
        if key in ("prompt", "negative", "seed") and lowered == "none":
            changes[key] = ()
        elif key == "mask" and lowered in ("alpha", "none"):
            changes[key] = ""
        elif key in ("image", "mask", "output"):
            node_id = value.partition(".")[0]
            if node_id not in graph:
                raise CustomWorkflowError(f"{key}={value}: the workflow has no node {node_id}.")
            if key == "output" and not _is_link(graph[node_id]["inputs"].get("images"), graph):
                raise CustomWorkflowError(f"output={value}: node {node_id} has no images input.")
            if key in ("image", "mask") and "image" not in graph[node_id]["inputs"]:
                raise CustomWorkflowError(f"{key}={value}: node {node_id} is not an image loader.")
            changes[key] = node_id
        else:
            changes[key] = _pairs(graph, key, value)
    return replace(mapping, **changes) if changes else mapping


def load(path: str | Path, inputs: str = "") -> CustomWorkflow:
    """Read, check and map the workflow file at ``path`` (``inputs``: the user's overrides)."""
    path = Path(str(path).strip().strip('"'))
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise CustomWorkflowError("The workflow file is too large (more than 8 MB).")
        data = path.read_bytes()
    except OSError as err:
        raise CustomWorkflowError(f"Can't read the workflow file {path}: {err.strerror or err}.") from None
    graph = parse(data)
    mapping = detect(graph)
    if inputs.strip():
        mapping = apply_inputs(graph, mapping, inputs)
    if mapping.image == mapping.mask:
        raise CustomWorkflowError("The picture and the mask can't go to the same node.")
    notes = []
    if not mapping.prompt:
        notes.append("no prompt input was found, so the workflow's own prompt is used")
    if not mapping.seed:
        notes.append("no seed was found, so every run uses the workflow's own seed")
    return CustomWorkflow(path.stem or "workflow", graph, mapping, str(path), tuple(notes))


# ----------------------------------------------------------------- running ----


def _free_id(graph: dict, wanted: str) -> str:
    node_id, n = wanted, 2
    while node_id in graph:
        node_id, n = f"{wanted}{n}", n + 1
    return node_id


def _rewire(graph: dict, old: list, new: list, skip: set[str]) -> int:
    """Point every input linked to ``old`` at ``new`` (except in ``skip``); returns how many."""
    count = 0
    for node_id, node in graph.items():
        if node_id in skip:
            continue
        for name, value in node["inputs"].items():
            if _is_link(value, graph) and value[0] == old[0] and value[1] == old[1]:
                node["inputs"][name] = list(new)
                count += 1
    return count


def _used(graph: dict, node_id: str, index: int) -> bool:
    return any(
        _is_link(v, graph) and v[0] == node_id and v[1] == index for n in graph.values() for v in n["inputs"].values()
    )


def _scale_output(graph: dict, source: list, kind: str, width: int, height: int, tag: str) -> list:
    """A node chain that resizes ``source`` (an IMAGE or a MASK) to ``width`` x ``height``."""
    if kind == "IMAGE":
        sid = _free_id(graph, f"{PREFIX}{tag}_scale")
        graph[sid] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": source,
                "upscale_method": "lanczos",
                "width": width,
                "height": height,
                "crop": "disabled",
            },
        }
        return [sid, 0]
    to_image = _free_id(graph, f"{PREFIX}{tag}_image")
    graph[to_image] = {"class_type": "MaskToImage", "inputs": {"mask": source}}
    scaled = _scale_output(graph, [to_image, 0], "IMAGE", width, height, tag)
    back = _free_id(graph, f"{PREFIX}{tag}_mask")
    graph[back] = {"class_type": "ImageToMask", "inputs": {"image": scaled, "channel": "red"}}
    return [back, 0]


def _scale_loader(graph: dict, node_id: str, width: int, height: int, tag: str) -> None:
    """Resize what a loader gives the rest of the graph to the working size."""
    outputs = (("IMAGE", 0), ("MASK", 1)) if graph[node_id]["class_type"] != "LoadImageMask" else (("MASK", 0),)
    for kind, index in outputs:
        if not _used(graph, node_id, index):
            continue
        before = set(graph)
        new = _scale_output(graph, [node_id, index], kind, width, height, f"{tag}{index}")
        _rewire(graph, [node_id, index], new, set(graph) - before)


def build(
    workflow: CustomWorkflow,
    image_ref: str,
    mask_ref: str,
    prompt: str,
    negative: str,
    seed: int,
    size: tuple[int, int],
    work_size: tuple[int, int],
    filename_prefix: str = "openfill/custom",
) -> dict:
    """The graph to queue: the user's workflow with OpenFill's picture, mask, prompt and
    seed filled in, inputs resized to ``work_size`` and the result to ``size``, saved by
    OpenFill's own SaveImage node (:data:`OUTPUT_NODE`). The user's own save nodes are
    left out, so nothing else is written to ComfyUI's output folder."""
    m = workflow.mapping
    graph = json.loads(json.dumps(workflow.graph))
    graph[m.image]["inputs"]["image"] = image_ref
    if m.mask:
        graph[m.mask]["inputs"]["image"] = mask_ref
    for node_id, name in m.prompt:
        graph[node_id]["inputs"][name] = prompt
    for node_id, name in m.negative:
        graph[node_id]["inputs"][name] = negative
    for node_id, name in m.seed:
        graph[node_id]["inputs"][name] = int(seed)
    width, height = int(size[0]), int(size[1])
    work_w, work_h = int(work_size[0]), int(work_size[1])
    if (work_w, work_h) != (width, height):
        for tag, node_id in (("in", m.image), ("mask_in", m.mask)):
            if node_id:
                _scale_loader(graph, node_id, work_w, work_h, tag)
    source = graph[m.output]["inputs"]["images"]
    for node_id in [nid for nid, n in graph.items() if n["class_type"] in OUTPUT_CLASSES]:
        if not any(_is_link(v, graph) and v[0] == node_id for n in graph.values() for v in n["inputs"].values()):
            del graph[node_id]  # the user's save and preview nodes: OpenFill saves the result itself
    out = _scale_output(graph, source, "IMAGE", width, height, "out")
    graph[_free_id(graph, OUTPUT_NODE)] = {
        "class_type": "SaveImage",
        "inputs": {"images": out, "filename_prefix": filename_prefix},
    }
    return graph


def output_node(graph: dict) -> str:
    """OpenFill's SaveImage node in a graph made by :func:`build`."""
    saves = [nid for nid, n in graph.items() if nid.startswith(OUTPUT_NODE) and n["class_type"] == "SaveImage"]
    return saves[-1] if saves else OUTPUT_NODE


def rgba_with_mask(rgba: bytes, mask: bytes) -> bytes:
    """The picture with its alpha replaced by the inverted selection (0 where selected)."""
    out = bytearray(rgba)
    out[3::4] = mask.translate(bytes(range(255, -1, -1)))
    return bytes(out)


def mask_rgba(mask: bytes) -> bytes:
    """The selection as RGBA: gray in the colour channels, and in the alpha inverted."""
    out = bytearray(len(mask) * 4)
    out[0::4] = mask
    out[1::4] = mask
    out[2::4] = mask
    out[3::4] = mask.translate(bytes(range(255, -1, -1)))
    return bytes(out)


def describe_file(path: str, inputs: str = "") -> str:
    """For the settings: what OpenFill found in the workflow file, or what is wrong with it."""
    if not str(path or "").strip():
        return ""
    try:
        wf = load(path, inputs)
    except CustomWorkflowError as err:
        return str(err)
    text = f"Found: {wf.describe()}."
    if wf.notes:
        text += " Note: " + "; ".join(wf.notes) + "."
    return text
