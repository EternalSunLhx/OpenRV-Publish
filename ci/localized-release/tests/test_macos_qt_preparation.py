"""Run the actual SDK preparation shell script with a small owned patch fixture.

No Xcode, network, installed Qt, user preferences or platform SDK is modified.
Production gets qt_fix.patch from the verified, materialized upstream release.
This offline fixture tests strict shell behavior, not a real Qt SDK or native GL.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
# Deliberately minimal configuration lines exercising the shell's real patch,
# precondition and postcondition commands. No upstream source/history is needed
# in the lightweight publishing checkout.
SDK_PATCH_FIXTURE = '''diff --git a/lib/cmake/Qt6/FindWrapOpenGL.cmake b/lib/cmake/Qt6/FindWrapOpenGL.cmake
--- a/lib/cmake/Qt6/FindWrapOpenGL.cmake
+++ b/lib/cmake/Qt6/FindWrapOpenGL.cmake
@@ -1,4 +1 @@
-find_library(WrapOpenGL_AGL NAMES AGL)
-set(__opengl_agl_fw_path "-framework AGL")
-target_link_libraries(WrapOpenGL::WrapOpenGL INTERFACE ${__opengl_agl_fw_path})
 target_link_libraries(WrapOpenGL::WrapOpenGL INTERFACE ${__opengl_fw_path})
diff --git a/mkspecs/common/mac.conf b/mkspecs/common/mac.conf
--- a/mkspecs/common/mac.conf
+++ b/mkspecs/common/mac.conf
@@ -1,3 +1,3 @@
 QMAKE_LIBS_DYNLOAD =
-QMAKE_LIBS_OPENGL = -framework OpenGL -framework AGL
+QMAKE_LIBS_OPENGL = -framework OpenGL
 QMAKE_LIBS_THREAD =
'''
BASH = shutil.which('bash') if os.name != 'nt' else None
if os.name == 'nt':
    for candidate in (Path('D:/OpenRV/.tools/msys64/usr/bin/bash.exe'),
                      Path('C:/Program Files/Git/bin/bash.exe')):
        if candidate.is_file():
            BASH = str(candidate)
            break


def patch_preimages(patch: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current = None
    in_hunk = False
    for line in patch.splitlines(keepends=True):
        if line.startswith('diff --git '):
            in_hunk = False
        elif line.startswith('--- a/'):
            current = line[len('--- a/'):].strip()
            files[current] = []
        elif line.startswith('@@ '):
            in_hunk = True
            start = int(re.match(r'@@ -(\d+)', line).group(1))
            while len(files[current]) < start - 1:
                files[current].append('\n')
        elif in_hunk and line.startswith((' ', '-')):
            files[current].append(line[1:])
    return {name: ''.join(lines) for name, lines in files.items()}


@unittest.skipUnless(BASH, 'A real Bash and patch implementation is required')
class MacQtPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='openrv-qt-patch-')
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.sdk = self.repo / 'downloaded SDK' / '6.5.3' / 'macos'
        self.script = self.repo / 'ci/localized-release/prepare-macos-qt.sh'
        self.script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'ci/localized-release/prepare-macos-qt.sh', self.script)
        patch = SDK_PATCH_FIXTURE
        (self.repo / 'qt_fix.patch').write_text(patch, encoding='utf-8', newline='\n')
        for name, content in patch_preimages(patch).items():
            path = self.sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        (self.sdk / 'mkspecs/qconfig.pri').write_text('QT_VERSION = 6.5.3\n', encoding='utf-8', newline='\n')

    def invoke(self, version='26.3', sdk=None):
        environment = os.environ.copy()
        if os.name == 'nt':
            # Use the same real Bash distribution's patch/grep, never WSL.
            binary = Path(BASH).resolve()
            prefix = binary.parent
            if prefix.name == 'bin' and prefix.parent.name != 'usr':
                alternative = prefix.parent / 'usr/bin'
                if alternative.is_dir():
                    prefix = alternative
            environment['PATH'] = str(prefix) + os.pathsep + environment.get('PATH', '')
            environment['MSYS2_ARG_CONV_EXCL'] = '*'
        return subprocess.run([BASH, str(self.script).replace('\\', '/'),
                               str(sdk or self.sdk).replace('\\', '/'), version],
                              cwd=self.repo, env=environment, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=15)

    def configs(self):
        return {name: (self.sdk / name).read_bytes() for name in (
            'lib/cmake/Qt6/FindWrapOpenGL.cmake', 'mkspecs/common/mac.conf')}

    def test_actual_shell_applies_fixture_then_is_idempotent(self):
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        changed = self.configs()
        self.assertTrue(all(b'AGL' not in value for value in changed.values()))
        self.assertIn(b'-framework OpenGL', changed['mkspecs/common/mac.conf'])
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already applied and verified', second.stdout)
        self.assertEqual(self.configs(), changed)

    def test_intel_16_4_keeps_original_sdk_configuration(self):
        before = self.configs()
        result = self.invoke('16.4')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.configs(), before)

    def test_wrong_root_and_missing_required_files_fail(self):
        result = self.invoke(sdk=self.sdk.parent)
        self.assertNotEqual(result.returncode, 0)
        (self.sdk / 'mkspecs/common/mac.conf').unlink()
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_wrong_qt_or_unpinned_xcode_version_fails(self):
        self.assertNotEqual(self.invoke('26.6').returncode, 0)
        (self.sdk / 'mkspecs/qconfig.pri').write_text('QT_VERSION = 6.8.3\n', encoding='utf-8')
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_missing_patch_fails(self):
        (self.repo / 'qt_fix.patch').unlink()
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_incompatible_hunk_fails_before_either_file_is_written(self):
        path = self.sdk / 'mkspecs/common/mac.conf'
        path.write_text(path.read_text().replace('QMAKE_LIBS_DYNLOAD', 'INCOMPATIBLE_CONTEXT'), encoding='utf-8')
        before = self.configs()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.configs(), before)

    def test_partly_applied_patch_is_not_reversed(self):
        self.assertEqual(self.invoke().returncode, 0)
        path = self.sdk / 'mkspecs/common/mac.conf'
        originals = patch_preimages((self.repo / 'qt_fix.patch').read_text(encoding='utf-8'))
        path.write_text(originals['mkspecs/common/mac.conf'], encoding='utf-8', newline='\n')
        before = self.configs()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(self.configs(), before)

    def test_empty_files_cannot_be_mistaken_for_an_applied_fix(self):
        for name in self.configs():
            (self.sdk / name).write_text('', encoding='utf-8')
        self.assertNotEqual(self.invoke().returncode, 0)


if __name__ == '__main__':
    unittest.main()
