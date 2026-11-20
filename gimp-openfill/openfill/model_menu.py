# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""The model menus of every OpenFill window (issue #14): what they list, in what order.

With a shared folder of many checkpoints a model menu can hold a hundred entries, taller
than the screen. Every picker (the PhotoCraft panel, its web page, the GIMP dialogs) lists:

- first the tool's own models in their usual order (the default, LaMa in Remove object, or
  the active model, then the catalog models);
- then, under one heading, the user's own model files (``local-`` ids, shared folders);

and lets the user type to filter it and hide models from it. Hidden models are kept in the
``hidden_models`` setting (a JSON list of model ids) and come back with "Show hidden". The
model in use is always listed, hidden or not. Pure standard library, no GIMP, no Tk.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import NamedTuple

OWN_FILES = "Your model files"
SEARCH_AT = 10  # a search field from this many models on
SEARCH_PLACEHOLDER = "Search models"
SHOW_HIDDEN = "Show hidden models"
HIDE = "Hide"
HIDE_MENU = "Hide models"
SHOW = "Show"
NO_MATCH = "No model matches"
CHOOSER_TITLE = "Choose a model"


class Row(NamedTuple):
    kind: str  # "head" (a heading) or "model"
    label: str
    model_id: str = ""
    hidden: bool = False


def own_file(model_id: str) -> bool:
    """A model file of the user's own (found in the models folder or a shared folder)."""
    return str(model_id).startswith("local-")


def parse_hidden(text: str) -> list[str]:
    """The ids in a ``hidden_models`` setting; anything unreadable is no hidden model."""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def dump_hidden(ids: Iterable[str]) -> str:
    out: list[str] = []
    for model_id in ids:
        if model_id and model_id not in out:
            out.append(str(model_id))
    return json.dumps(out) if out else ""


def set_hidden(text: str, model_id: str, hidden: bool) -> str:
    """``text`` (a ``hidden_models`` setting) with ``model_id`` hidden or shown again."""
    ids = [i for i in parse_hidden(text) if i != model_id]
    if hidden and model_id:
        ids.append(model_id)
    return dump_hidden(ids)


def matches(label: str, query: str) -> bool:
    """Every word of ``query`` is in ``label`` (any case): "sdxl real" finds "RealVisXL (SDXL)"."""
    text = label.casefold()
    return all(word in text for word in query.casefold().split())


def arrange(
    options: Iterable[tuple[str, str]],
    hidden: Iterable[str] = (),
    keep: str = "",
    show_hidden: bool = False,
    query: str = "",
) -> list[Row]:
    """The rows of a model menu from ``options`` ((model id, label), in the tool's order).

    The tool's own models come first, the user's model files after a heading. Hidden ids
    are left out unless ``show_hidden``; ``keep`` (the model in use) is always listed.
    ``query`` keeps the models whose label matches; a heading with nothing under it goes."""
    hidden = set(hidden)
    tool: list[Row] = []
    files: list[Row] = []
    for model_id, label in options:
        is_hidden = model_id in hidden
        if is_hidden and not show_hidden and model_id != keep:
            continue
        if query and not matches(label, query):
            continue
        (files if own_file(model_id) else tool).append(Row("model", label, model_id, is_hidden))
    if files and tool:
        return [*tool, Row("head", OWN_FILES), *files]
    return tool or files


def hidden_count(options: Iterable[tuple[str, str]], hidden: Iterable[str], keep: str = "") -> int:
    """How many of ``options`` are hidden from the menu (the model in use never is)."""
    hidden = set(hidden)
    return sum(1 for model_id, _label in options if model_id in hidden and model_id != keep)
