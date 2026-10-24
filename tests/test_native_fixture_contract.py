import importlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
native_integration = importlib.import_module('native_integration')


class NativeFixtureContractTests(unittest.TestCase):
    def test_maria_socket_contract_accepts_only_a_string_path(self):
        self.assertEqual(native_integration.require_maria_socket_path('/tmp/paymenter.sock'), '/tmp/paymenter.sock')
        for invalid in (('/tmp/paymenter.sock', 3307), pathlib.Path('/tmp/paymenter.sock')):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                native_integration.require_maria_socket_path(invalid)


if __name__ == '__main__':
    unittest.main()
