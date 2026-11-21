"""The owned-resource manifest records staged bytes, not Windows checkout state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[3] / 'tools/refresh_patch_manifest.py'
spec = importlib.util.spec_from_file_location('openrv_refresh_manifest', SCRIPT)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class PatchManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='openrv-owned-manifest-')
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git('init', '--initial-branch=main')
        self.git('config', 'core.autocrlf', 'false')
        files = {'.gitattributes': b'* text=auto eol=lf\n*.patch -text\n',
                 'patches/openrv-core.patch': b'fixed core patch\n',
                 'patches/annotate-beta.patch': b'fixed optional patch\n',
                 'overlay/src/owned.cpp': b'// staged native code\r\n',
                 refresh.CATALOG: b'<TS/>\n',
                 '.github/workflows/release.yml': b'name: Own release\n',
                 'ci/owned.py': b'# owned tool\n'}
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.git('add', '.')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT)

    def test_staged_line_endings_and_unstaged_edits_cannot_change_source_identity(self):
        first = refresh.build_manifest(self.repo)
        (self.repo / 'overlay/src/owned.cpp').write_bytes(b'// unrelated unstaged edit\n')
        self.assertEqual(refresh.build_manifest(self.repo), first)
        manifest = json.loads(first)
        item = next(entry for entry in manifest['owned_files'] if entry['source'].startswith('overlay/'))
        self.assertEqual(item['destination'], 'src/owned.cpp')
        self.assertEqual(item['sha256'], hashlib.sha256(b'// staged native code\n').hexdigest())
        self.assertFalse(item['replace'])

    def test_catalog_is_the_explicit_replacement_and_manifest_has_no_self_digest(self):
        path = self.repo / refresh.CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(refresh.build_manifest(self.repo))
        self.git('add', refresh.CONFIG)
        manifest = json.loads(refresh.build_manifest(self.repo))
        self.assertEqual(path.read_bytes(), refresh.build_manifest(self.repo))
        replaced = [entry for entry in manifest['owned_files'] if entry['replace']]
        self.assertEqual([entry['source'] for entry in replaced], [refresh.CATALOG])
        self.assertEqual(replaced[0]['destination'], refresh.CATALOG_TARGET)
        self.assertNotIn(refresh.CONFIG, {entry['source'] for entry in manifest['owned_files']})
        self.assertNotIn('.github/workflows/release.yml', {entry['source'] for entry in manifest['owned_files']})

    def test_missing_staged_catalog_fails_instead_of_silently_dropping_translations(self):
        self.git('rm', '--cached', refresh.CATALOG)
        with self.assertRaisesRegex(ValueError, 'complete translation catalog'):
            refresh.build_manifest(self.repo)

    def test_reviewed_patches_use_staged_digests_and_only_annotate_beta_is_optional(self):
        manifest = json.loads(refresh.build_manifest(self.repo))
        core = 'patches/openrv-core.patch'
        self.assertEqual(manifest['patches'], [
            {'path': core, 'optional_component': None,
             'sha256': hashlib.sha256(b'fixed core patch\n').hexdigest()},
            {'path': 'patches/annotate-beta.patch', 'optional_component': 'src/plugins/rv-packages/annotate_beta',
             'sha256': hashlib.sha256(b'fixed optional patch\n').hexdigest()},
        ])
        (self.repo / core).write_bytes(b'unstaged alternative patch\n')
        self.assertEqual(json.loads(refresh.build_manifest(self.repo)), manifest)
        (self.repo / core).write_bytes(b'fixed core patch\n')
        self.git('rm', '--cached', core)
        with self.assertRaisesRegex(ValueError, 'Missing staged reviewed patch'):
            refresh.build_manifest(self.repo)


if __name__ == '__main__':
    unittest.main()
