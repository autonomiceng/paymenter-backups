"""Pin the refusal codes accepted from the native writer."""
import ast
import pathlib
import unittest

from paymenter_backups import cli


class WriterRefusalCodeTests(unittest.TestCase):
    def test_both_writer_paths_share_the_pinned_refusal_membership(self):
        expected = frozenset({
            "application-unavailable", "archive-key-change-busy", "billing-settings-unreadable",
            "config-changed", "credentials-invalid", "credentials-unreadable",
            "database-connection-failed", "database-connection-invalid", "database-driver-unsupported",
            "database-pdo-driver-unavailable", "database-prefix-unsupported", "database-tls-unsupported",
            "document-roots-invalid", "extension-uninstalled", "installation-mismatch", "installation-missing",
            "maintenance-probe-credentials-invalid", "maintenance-probe-origin-invalid",
            "maintenance-probe-origin-mismatch", "native-secret-row-refused", "private-file-invalid",
            "private-pipe-required", "private-root-invalid", "private-root-unavailable", "unsupported-database",
        })
        self.assertEqual(cli.WRITER_REFUSAL_CODES, expected)
        self.assertIsInstance(cli.WRITER_REFUSAL_CODES, frozenset)

        tree = ast.parse(pathlib.Path(cli.__file__).read_text())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for function in ("native_settings", "set_config"):
            references = [
                node for node in ast.walk(functions[function])
                if isinstance(node, ast.Name) and node.id == "WRITER_REFUSAL_CODES" and isinstance(node.ctx, ast.Load)
            ]
            self.assertTrue(references, function)


if __name__ == "__main__":
    unittest.main()
