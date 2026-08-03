from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def node_binary() -> str | None:
    configured = os.environ.get("YT_LIBRARY_NODE", "").strip()
    if configured:
        return configured
    discovered = shutil.which("node")
    if discovered:
        return discovered
    bundled = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    )
    return str(bundled) if bundled.exists() else None


class JavaScriptAssetTests(unittest.TestCase):
    @unittest.skipUnless(node_binary(), "Node.js is required for browser asset tests")
    def test_browser_assets_parse_and_shared_helpers_behave(self) -> None:
        result = subprocess.run(
            [node_binary(), "--test", "tests/js/browser-assets.test.js"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
