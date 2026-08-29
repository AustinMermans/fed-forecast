#!/usr/bin/env python3
"""Download and validate the public Pages bundle from one trusted origin."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from validate_public_site import validate


REQUIRED = (
    "index.html",
    "methodology.html",
    "assets/styles.css",
    "assets/dashboard.js",
    "assets/branch-decomposition.js",
    "assets/methodology.js",
    "assets/methodology.css",
    "data/dashboard.json",
    "data/forecast-replay.json",
    "data/evidence-summary.json",
)


def download_and_validate(base_url: str, expected_evidence_sha256: str) -> None:
    base = base_url.rstrip("/") + "/"
    origin = urlparse(base)
    if origin.scheme not in {"http", "https"} or not origin.netloc:
        raise ValueError("deployment base URL must be HTTP(S)")
    if len(expected_evidence_sha256) != 64:
        raise ValueError("expected evidence SHA-256 is invalid")
    with tempfile.TemporaryDirectory(prefix="fed-forecast-deployed-") as directory:
        mirror = Path(directory)
        for relative in REQUIRED:
            url = urljoin(base, relative)
            with urlopen(url, timeout=20) as response:  # noqa: S310 - origin is checked above
                final = urlparse(response.geturl())
                if (final.scheme, final.netloc) != (origin.scheme, origin.netloc):
                    raise ValueError(f"cross-origin redirect for {relative}")
                content = response.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError(f"deployed file is unexpectedly large: {relative}")
            target = mirror / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        evidence = mirror / "data" / "evidence-summary.json"
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != expected_evidence_sha256:
            raise ValueError("deployed evidence hash does not match the release")
        html = (mirror / "index.html").read_text(encoding="utf-8")
        branch = "assets/branch-decomposition.js?v=stage1a-2"
        dashboard = "assets/dashboard.js?v=stage1a-2"
        if branch not in html or dashboard not in html or html.index(branch) >= html.index(dashboard):
            raise ValueError("deployed script order/cache contract is invalid")
        validate(mirror)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--expected-evidence-sha256", required=True)
    args = parser.parse_args()
    download_and_validate(args.base_url, args.expected_evidence_sha256)
    print(f"validated deployed site {args.base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
