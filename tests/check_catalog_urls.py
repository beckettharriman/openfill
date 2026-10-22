# SPDX-License-Identifier: Apache-2.0
"""Network check (not a unit test): HEAD every URL in models/catalog.json.

    HF_TOKEN=hf_xxx python tests/check_catalog_urls.py

Gated Hugging Face files answer 401/403 without a token; that counts as
"resolves" because the host exists and the gating is expected. The token is
read from the HF_TOKEN environment variable (``--token`` still works but puts
it in the process list) and is only sent to https Hugging Face hosts, never
to GitHub or license pages and never on a redirect.
"""

import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gimp-openfill"))
from openfill.catalog import load_catalog  # noqa: E402
from openfill.downloads import token_allowed  # noqa: E402


def head(url: str, token: str | None = None) -> tuple[int | None, str | None]:
    """HEAD ``url``; returns ``(status, size header)`` or ``(None, error text)``."""
    req = Request(url, headers={"User-Agent": "gimp-openfill url check"}, method="HEAD")
    if token and token_allowed(url):
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Length") or r.headers.get("X-Linked-Size")
    except HTTPError as e:
        return e.code, e.headers.get("X-Linked-Size")
    except URLError as e:
        return None, str(e)


def main() -> None:
    """Check every page, license and file URL; exit 1 when any is broken."""
    token = sys.argv[sys.argv.index("--token") + 1] if "--token" in sys.argv else os.environ.get("HF_TOKEN")
    entries = load_catalog(Path(__file__).resolve().parent.parent / "models" / "catalog.json")
    bad = 0
    for e in entries:
        for label, url in (("page", e.page), ("license", e.license_url)):
            status, _ = head(url, token)
            ok = status in (200, 301, 302, 303, 307, 308)
            bad += not ok
            print(f"{'ok ' if ok else 'BAD'} {e.id:28s} {label:8s} {status} {url}")
        for f in e.files:
            status, size = head(f.url, token)
            ok = status in (200, 302) or (f.gated and status in (401, 403))
            size_ok = size is None or f.size is None or int(size) == f.size or status in (401, 403)
            bad += not (ok and size_ok)
            print(
                f"{'ok ' if ok and size_ok else 'BAD'} {e.id:28s} {f.role:8s} {status} size={size} expected={f.size} {f.url}"
            )
    print("problems:", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
