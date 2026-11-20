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
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gimp-openfill"))
from openfill.catalog import load_catalog  # noqa: E402
from openfill.downloads import token_allowed  # noqa: E402


class _NoRedirect(HTTPRedirectHandler):
    """Stop at Hugging Face's 302: it carries X-Linked-Size and X-Linked-ETag, the CDN answer does not."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.host == "huggingface.co":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open = build_opener(_NoRedirect).open


def head(url: str, token: str | None = None) -> tuple[int | None, str | None, str | None]:
    """HEAD ``url``; returns ``(status, size header, X-Linked-ETag)`` or ``(None, error text, None)``.

    Hugging Face's X-Linked-ETag of an LFS file is its SHA-256."""
    req = Request(url, headers={"User-Agent": "gimp-openfill url check"}, method="HEAD")
    if token and token_allowed(url):
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with _open(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Length") or r.headers.get("X-Linked-Size"), _etag(r.headers)
    except HTTPError as e:
        return e.code, e.headers.get("X-Linked-Size"), _etag(e.headers)
    except (URLError, OSError) as e:  # OSError: a read timeout is not wrapped in URLError
        return None, str(e), None


def _etag(headers) -> str | None:
    value = (headers.get("X-Linked-ETag") or "").strip().strip('"').lower()
    return value if len(value) == 64 else None


def main() -> None:
    """Check every page, license and file URL; exit 1 when any is broken."""
    token = sys.argv[sys.argv.index("--token") + 1] if "--token" in sys.argv else os.environ.get("HF_TOKEN")
    entries = load_catalog(Path(__file__).resolve().parent.parent / "models" / "catalog.json")
    bad = 0
    for e in entries:
        for label, url in (("page", e.page), ("license", e.license_url)):
            status, _, _ = head(url, token)
            ok = status in (200, 301, 302, 303, 307, 308)
            bad += not ok
            print(f"{'ok ' if ok else 'BAD'} {e.id:28s} {label:8s} {status} {url}")
        for f in e.files:
            status, size, etag = head(f.url, token)
            ok = status in (200, 302) or (f.gated and status in (401, 403))
            size_ok = size is None or f.size is None or int(size) == f.size or status in (401, 403)
            sha_ok = etag is None or etag == f.sha256
            bad += not (ok and size_ok and sha_ok)
            print(
                f"{'ok ' if ok and size_ok and sha_ok else 'BAD'} {e.id:28s} {f.role:8s} {status} size={size} "
                f"expected={f.size} sha256={'matches' if etag and sha_ok else 'DIFFERS' if etag else 'not sent'} {f.url}"
            )
    print("problems:", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
