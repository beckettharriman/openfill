# SPDX-License-Identifier: Apache-2.0
"""Network check (not a unit test): every ComfyUI archive Download ComfyUI can fetch
(the NVIDIA build, the one for older NVIDIA cards and the one for AMD cards) answers
at its pinned URL with its pinned size.

    python tests/check_comfy_archives.py
"""

import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gimp-openfill"))
from openfill.comfy_setup import load_manifest  # noqa: E402


def head(url: str) -> tuple[int | None, int | None]:
    """HEAD ``url`` (following GitHub's redirects); ``(status, Content-Length)``."""
    req = Request(url, headers={"User-Agent": "openfill archive check"}, method="HEAD")
    try:
        with urlopen(req, timeout=60) as r:
            length = r.headers.get("Content-Length")
            return r.status, int(length) if length else None
    except HTTPError as e:
        return e.code, None
    except URLError:
        return None, None


def main() -> int:
    m = load_manifest(Path(__file__).resolve().parent.parent / "models" / "comfy_setup.json")
    bad = 0
    for label, chosen in (("nvidia", m), ("older nvidia", m.for_older_gpus()), ("amd", m.for_amd())):
        status, size = head(chosen.archive_url)
        ok = status == 200 and size == chosen.archive_size
        bad += not ok
        print(
            f"{'ok ' if ok else 'BAD'} {label:13s} {status} size={size} expected={chosen.archive_size} {chosen.archive_url}"
        )
    print("problems:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
