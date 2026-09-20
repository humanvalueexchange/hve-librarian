from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import pdf_collector


class PdfCollectorPathTests(unittest.TestCase):
    def test_accepts_profile_attachment_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "profiles" / "hve-librarian" / "cache" / "documents"
            cache.mkdir(parents=True)
            pdf = cache / "course.pdf"
            pdf.write_bytes(b"%PDF-test")

            with mock.patch.object(pdf_collector, "_allowed_attachment_roots", return_value=(cache,)):
                self.assertEqual(pdf_collector._safe_attachment_path(str(pdf)), pdf.resolve())

    def test_rejects_path_outside_approved_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            approved = Path(tmpdir) / "approved"
            approved.mkdir()
            outside = Path(tmpdir) / "outside.pdf"
            outside.write_bytes(b"%PDF-test")

            with mock.patch.object(pdf_collector, "_allowed_attachment_roots", return_value=(approved,)):
                with self.assertRaisesRegex(
                    pdf_collector.PdfCollectorError,
                    "approved Hermes attachment cache",
                ):
                    pdf_collector._safe_attachment_path(str(outside))


if __name__ == "__main__":
    unittest.main()
