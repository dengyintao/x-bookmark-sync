"""Release metadata must agree before expensive package builds start."""
import importlib.util
from pathlib import Path
import unittest


class ReleaseMetadata(unittest.TestCase):
    def test_runtime_python_and_fpk_versions_agree(self):
        path = Path(__file__).parents[1]/'scripts/build_release.py'
        spec = importlib.util.spec_from_file_location('release_builder',path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        version, extension, manifest = module.version_info()
        self.assertEqual(manifest['version'],version)
        self.assertTrue(extension)
        with self.assertRaises(ValueError):module.version_info('v0.0.0-invalid')

    def test_fpk_password_is_optional_on_install_and_upgrade(self):
        import json
        root = Path(__file__).parents[1]/'packaging/fnos/wizard'
        for name in ('install', 'upgrade'):
            wizard = json.loads((root/name).read_text())
            password = next(item for step in wizard for item in step['items'] if item.get('field') == 'wizard_admin_password')
            self.assertFalse(any(rule.get('required') for rule in password['rules']))
