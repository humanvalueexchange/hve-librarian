from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.x402_public import PublicProjectionError, validate_public_repository


class X402PublicPublisherTests(unittest.TestCase):
    def test_rejects_unexpected_repository_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/wrong.git"],
                check=True,
            )
            with self.assertRaises(PublicProjectionError):
                validate_public_repository(repo)

    def test_accepts_exact_public_repository_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/HansHWestphal/x402-watch.git",
                ],
                check=True,
            )
            validate_public_repository(repo)


if __name__ == "__main__":
    unittest.main()
