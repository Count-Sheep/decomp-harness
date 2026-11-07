import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_m2c_patch as m2c_patch


class M2cPatchTests(unittest.TestCase):
    def setUp(self):
        single = patch.object(m2c_patch, 'PATCHES', (m2c_patch.PATCH,))
        single.start()
        self.addCleanup(single.stop)

    def result(self, code=0, text="", error=""):
        return subprocess.CompletedProcess([], code, text, error)

    @patch.object(m2c_patch.subprocess, "run")
    def test_check_does_not_apply(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result()]
        self.assertEqual(m2c_patch.apply(Path("tool"), True), "ready")
        self.assertEqual(run.call_count, 2)
        self.assertIn("--check", run.call_args.args[0])

    @patch.object(m2c_patch.subprocess, "run")
    def test_apply_is_explicit_and_checked(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result(), self.result()]
        self.assertEqual(m2c_patch.apply(Path("tool")), "applied")
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("--check", run.call_args.args[0])

    @patch.object(m2c_patch.subprocess, "run")
    def test_already_applied_is_idempotent(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result(1), self.result()]
        self.assertEqual(m2c_patch.apply(Path("tool")), "already_applied")
        self.assertIn("--reverse", run.call_args.args[0])
        self.assertIn("--check", run.call_args.args[0])

    @patch.object(m2c_patch.subprocess, "run")
    def test_conflicting_local_changes_are_not_reset(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result(1, error="conflict"), self.result(1)]
        with self.assertRaisesRegex(ValueError, "conflicts"):
            m2c_patch.apply(Path("tool"))
        self.assertEqual(run.call_count, 3)

    @patch.object(m2c_patch.subprocess, "run")
    def test_wrong_revision_is_untouched(self, run):
        run.return_value = self.result(text="other-revision")
        with self.assertRaisesRegex(ValueError, "left unchanged"):
            m2c_patch.apply(Path("tool"))
        self.assertEqual(run.call_count, 1)

    @patch.object(m2c_patch.subprocess, 'run')
    def test_partial_install_applies_only_missing_patch(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result(1), self.result(),
                           self.result(), self.result()]
        with patch.object(m2c_patch, 'PATCHES', (Path('first.patch'), Path('second.patch'))):
            self.assertEqual(m2c_patch.apply(Path('tool')), 'applied')
        self.assertTrue(run.call_args.args[0][-1].endswith('second.patch'))
        self.assertFalse(any(arg.endswith('first.patch') for arg in run.call_args.args[0]))

    @patch.object(m2c_patch.subprocess, 'run')
    def test_all_patches_preflight_before_any_apply(self, run):
        run.side_effect = [self.result(text=m2c_patch.BASE_REVISION), self.result(),
                           self.result(1, error='conflict'), self.result(1)]
        with patch.object(m2c_patch, 'PATCHES', (Path('first.patch'), Path('second.patch'))):
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                m2c_patch.apply(Path('tool'))
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all('--check' in call.args[0] for call in run.call_args_list[1:]))


class DependentM2cPatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name)
        self.root = parent / 'tool'
        self.root.mkdir()
        self.git('init', '--quiet')
        self.git('config', 'core.autocrlf', 'false')
        (self.root / 'value.txt').write_text('before\n', encoding="utf-8", newline="\n")
        self.git('add', 'value.txt')
        self.git('-c', 'user.name=Patch Test', '-c', 'user.email=patch@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                 'commit', '--quiet', '-m', 'baseline')
        revision = self.git('rev-parse', 'HEAD').stdout.strip()
        first, second = parent / 'first.patch', parent / 'second.patch'
        header = 'diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n'
        first.write_text(header + '-before\n+middle\n', encoding="utf-8", newline="\n")
        second.write_text(header + '-middle\n+after\n' +
                          'diff --git a/new.txt b/new.txt\nnew file mode 100644\n' +
                          '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n', encoding="utf-8", newline="\n")
        self.patches = (first, second)
        for name, value in [('BASE_REVISION', revision), ('PATCHES', self.patches)]:
            mocked = patch.object(m2c_patch, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args],
                              capture_output=True, text=True, check=True)

    def test_dependent_fresh_install_is_atomic_and_idempotent(self):
        self.assertEqual(m2c_patch.apply(self.root), 'applied')
        self.assertEqual((self.root / 'value.txt').read_text(encoding="utf-8"), 'after\n')
        self.assertEqual((self.root / 'new.txt').read_text(encoding="utf-8"), 'new\n')
        self.assertFalse((self.root / 'after').exists())
        self.assertEqual(m2c_patch.apply(self.root), 'already_applied')

    def test_check_leaves_original_tree_unchanged(self):
        self.assertEqual(m2c_patch.apply(self.root, True), 'ready')
        self.assertEqual((self.root / 'value.txt').read_text(encoding="utf-8"), 'before\n')
        self.assertFalse((self.root / 'new.txt').exists())

    def test_upgrade_of_installed_prefix(self):
        self.git('apply', str(self.patches[0]))
        self.assertEqual(m2c_patch.apply(self.root), 'applied')
        self.assertEqual((self.root / 'value.txt').read_text(encoding="utf-8"), 'after\n')

    def test_conflict_does_not_leave_first_patch_applied(self):
        (self.root / 'value.txt').write_text('user edit\n', encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            m2c_patch.apply(self.root)
        self.assertEqual((self.root / 'value.txt').read_text(encoding="utf-8"), 'user edit\n')
        self.assertFalse((self.root / 'new.txt').exists())

    def test_crlf_converted_patch_checkout_applies_byte_exact(self):
        for path in self.patches:
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(m2c_patch.apply(self.root), 'applied')
        self.assertEqual((self.root / 'value.txt').read_bytes(), b'after\n')
        self.assertEqual((self.root / 'new.txt').read_bytes(), b'new\n')

    def test_dependent_series_preserves_carriage_returns_in_patched_content(self):
        (self.root / 'crlf.txt').write_bytes(b'one\r\n')
        self.git('add', 'crlf.txt')
        self.git('-c', 'user.name=Patch Test', '-c', 'user.email=patch@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'crlf')
        revision = self.git('rev-parse', 'HEAD').stdout.strip()
        header = b'diff --git a/crlf.txt b/crlf.txt\n--- a/crlf.txt\n+++ b/crlf.txt\n@@ -1 +1 @@\n'
        self.patches[0].write_bytes(header + b'-one\r\n+two\r\n')
        self.patches[1].write_bytes(header + b'-two\r\n+three\r\n')
        with patch.object(m2c_patch, 'BASE_REVISION', revision):
            self.assertEqual(m2c_patch.apply(self.root), 'applied')
        self.assertEqual((self.root / 'crlf.txt').read_bytes(), b'three\r\n')


class ShippedPatchTests(unittest.TestCase):
    def test_shipped_series_is_ordered_lf_and_parseable(self):
        names = [path.name for path in m2c_patch.PATCHES]
        self.assertEqual(names, sorted(names))
        self.assertEqual(m2c_patch.PATCHES[0], m2c_patch.PATCH)
        with tempfile.TemporaryDirectory() as name, m2c_patch.patch_files() as files:
            for file in files:
                self.assertIsNone(m2c_patch._lf_only(Path(file).read_bytes()), file)
                result = subprocess.run(['git', '-C', name, 'apply', '--numstat', file],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
