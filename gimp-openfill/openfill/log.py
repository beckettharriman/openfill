# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""File logging shared by the plug-in process and the supervisor."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-5s [%(process)d %(name)s] %(message)s"

# Hugging Face user tokens ("hf_" + 30-40 letters/digits) and any bearer credential.
_SECRET_PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\"?hf_token\"?\s*[:=]\s*\"?)[^\s\",}]+"),
)


def redact(text: str) -> str:
    """Replace anything that looks like an access token with ``[redacted]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs tokens from the formatted message (defence in depth:
    no code path is meant to log a token in the first place)."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite ``record`` in place; never drops it."""
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not kill logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


def get_logger(path: Path, name: str = "openfill", debug: bool = False) -> logging.Logger:
    """A logger writing to ``path`` (rotating, 2 MB x 3), with token redaction.

    Calling it again with the same path reuses the existing handler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    wanted = str(Path(path))
    for existing in logger.handlers:
        if getattr(existing, "baseFilename", None) == wanted:
            return logger
    handler: logging.Handler
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(wanted, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
