"""Offline tests for the website asset boundary; no user Git index changes."""
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('packager', ROOT / '.github/scripts/prepare_static_site.py')
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class PackagingTests(unittest.TestCase):
    def test_loader_is_required_and_backend_is_excluded(self):
        tracked = set(packager.REQUIRED_FILES) | {
            'f1.jpeg', 'gallery.json', 'backend/onedrive-publisher/function_app.py',
            'backend/onedrive-publisher/private.png', 'photos/onedrive/spotify/a.jpg',
            'tests/gallery-source.test.cjs', 'docs/onedrive-beta-setup.md',
            '.work/tool.js', 'outputs/report.md', '.env', '.secret.png'
        }
        selected = set(packager.select_files(tracked))
        self.assertEqual(selected, set(packager.REQUIRED_FILES) | {'f1.jpeg'})
        self.assertIn('gallery-source.js', selected)
        with self.assertRaises(ValueError):
            packager.select_files(tracked - {'gallery-source.js'})

    def test_complete_package_keeps_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as parent:
            repo = pathlib.Path(parent) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            for name in packager.REQUIRED_FILES:
                (repo / name).write_bytes((ROOT / name).read_bytes())
            (repo / 'f1.jpeg').write_bytes(b'unchanged-original-fixture')
            (repo / 'backend').mkdir()
            (repo / 'backend/local.settings.json').write_text('private')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            out = pathlib.Path(parent) / 'package'
            selected = packager.package(repo, out)
            self.assertEqual(len(selected), len(packager.REQUIRED_FILES) + 1)
            self.assertTrue((out / 'assets/gallery-source.js').is_file())
            self.assertFalse((out / 'assets/gallery.json').exists())
            self.assertEqual((out / 'assets/f1.jpeg').read_bytes(), b'unchanged-original-fixture')
            self.assertEqual((out / 'entrypoint/index.html').read_bytes(), (ROOT / 'index.html').read_bytes())
            actual = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()}
            expected = {'entrypoint/index.html'} | {'assets/' + n for n in selected if n != 'index.html'}
            self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
