from __future__ import annotations

import os
import unittest
from pathlib import Path

from tools import knowledge_layer_client


class KnowledgeLayerClientTests(unittest.TestCase):
    def test_cli_command_targets_public_independent_interface(self) -> None:
        command = knowledge_layer_client.cli_command(
            "status",
            root=Path("/tmp/test-library"),
        )

        self.assertEqual(command[1:4], ["-m", "hve_knowledge_layer.cli", "--root"])
        self.assertIn("/tmp/test-library", command)
        self.assertNotIn("hanshermesagent/knowledge/layer", " ".join(command))

    def test_cli_environment_points_to_installed_layer(self) -> None:
        environment = knowledge_layer_client.cli_environment()

        self.assertIn("/opt/hve-knowledge-layer/current/src", environment["PYTHONPATH"])
        self.assertEqual(
            environment["HVE_KNOWLEDGE_CONFIG"],
            "/opt/hve-knowledge-layer/current/config/knowledge-layer/knowledge-layer.yaml",
        )
        self.assertEqual(environment.get("PATH"), os.environ.get("PATH"))

    def test_cli_command_can_request_machine_readable_output(self) -> None:
        command = knowledge_layer_client.cli_command(
            "--json",
            "index",
            "--chunk-file",
            "/tmp/chunks.jsonl",
            "--manifest",
            "/tmp/manifest.json",
            root=Path("/tmp/test-library"),
        )

        self.assertIn("--json", command)
