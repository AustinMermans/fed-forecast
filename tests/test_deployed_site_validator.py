from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("deployed_validator", ROOT / "scripts" / "validate_deployed_site.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DeployedSiteValidatorTests(unittest.TestCase):
    class Response:
        def __init__(self, url, content):
            self.url, self.content = url, content
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def geturl(self): return self.url
        def read(self, limit): return self.content[:limit]

    def test_downloaded_mirror_passes_and_wrong_hash_fails(self) -> None:
        def fetch(url, timeout):
            del timeout
            relative = url.removeprefix("https://example.test/")
            return self.Response(url, (ROOT / "site" / relative).read_bytes())

        digest = hashlib.sha256((ROOT / "site" / "data" / "evidence-summary.json").read_bytes()).hexdigest()
        with patch.object(MODULE, "urlopen", fetch):
            MODULE.download_and_validate("https://example.test/", digest)
            with self.assertRaisesRegex(ValueError, "evidence hash"):
                MODULE.download_and_validate("https://example.test/", "0" * 64)

    def test_missing_malformed_stale_and_redirected_evidence_fail_closed(self) -> None:
        evidence_bytes = (ROOT / "site/data/evidence-summary.json").read_bytes()
        digest = hashlib.sha256(evidence_bytes).hexdigest()

        def fetch(mode):
            def implementation(url, timeout):
                del timeout
                relative = url.removeprefix("https://example.test/")
                if relative == "data/evidence-summary.json":
                    if mode == "missing": raise FileNotFoundError(relative)
                    if mode == "malformed": return self.Response(url, b'{"schema_version":2,"xss":"<img onerror=1>"}')
                    if mode == "redirect": return self.Response("https://evil.test/evidence-summary.json", evidence_bytes)
                content = (ROOT / "site" / relative).read_bytes()
                if mode == "stale" and relative == "data/dashboard.json":
                    dashboard = json.loads(content)
                    dashboard["evidence_summary"]["generated_at"] = "2000-01-01T00:00:00Z"
                    content = json.dumps(dashboard).encode()
                return self.Response(url, content)
            return implementation

        for mode, pattern in (("missing", "evidence-summary"), ("malformed", "evidence"), ("stale", "stale"), ("redirect", "redirect")):
            with self.subTest(mode=mode), patch.object(MODULE, "urlopen", fetch(mode)):
                with self.assertRaisesRegex((ValueError, FileNotFoundError), pattern):
                    MODULE.download_and_validate("https://example.test/", digest)

    def test_public_validator_rejects_duplicate_json_and_every_contract_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "site"
            shutil.copytree(ROOT / "site", mirror)
            dashboard_path = mirror / "data/dashboard.json"
            original = dashboard_path.read_bytes()
            dashboard_path.write_bytes(original.replace(b"{", b'{"schema_version":2,', 1))
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                MODULE.validate(mirror)
            dashboard_path.write_bytes(original)
            dashboard = json.loads(original)
            mutations = {
                "url": "data/wrong.json", "schema_version": 3, "sha256": "0" * 64,
                "generated_at": "2000-01-01T00:00:00Z", "legacy_model_sha256": "0" * 64,
                "legacy_cutoff_at": "2000-01-01T00:00:00Z",
            }
            for field, replacement in mutations.items():
                with self.subTest(field=field):
                    changed = json.loads(original)
                    changed["evidence_summary"][field] = replacement
                    dashboard_path.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError, "evidence contract"):
                        MODULE.validate(mirror)
            changed = json.loads(original)
            changed["evidence_summary"]["extra"] = True
            dashboard_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "evidence contract"):
                MODULE.validate(mirror)

    def test_public_validator_rejects_matching_hash_numeric_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "site"
            shutil.copytree(ROOT / "site", mirror)
            evidence_path = mirror / "data/evidence-summary.json"
            evidence = evidence_path.read_bytes().replace(b'"transition_count": 13', b'"transition_count": 1e400', 1)
            evidence_path.write_bytes(evidence)
            dashboard_path = mirror / "data/dashboard.json"
            dashboard = json.loads(dashboard_path.read_bytes())
            dashboard["evidence_summary"]["sha256"] = hashlib.sha256(evidence).hexdigest()
            dashboard_path.write_text(json.dumps(dashboard))
            with self.assertRaisesRegex(ValueError, "non-finite"):
                MODULE.validate(mirror)


if __name__ == "__main__":
    unittest.main()
