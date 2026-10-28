from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class LinkSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo with spaces"
        (self.repository / "scripts").mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "scripts" / "link_skills.py"
        self.script = self.repository / "scripts" / source.name
        shutil.copyfile(source, self.script)
        for name in ["one", "two"]:
            folder = self.repository / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("test fixture", encoding="utf-8")
        self.target = self.root / "target"

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(self.script), "--target", str(self.target), *args],
                              cwd=self.root, capture_output=True, text=True)

    def test_preview_creates_nothing(self):
        result = self.run_script("--all", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.target.exists())
        self.assertIn("DRY-RUN LINK", result.stdout)

    def test_link_repeated_install_and_live_source_changes(self):
        self.assertEqual(self.run_script("one").returncode, 0)
        link = self.target / "one"
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.target / "two").exists())
        self.assertEqual(link.resolve(), (self.repository / "skills" / "one").resolve())
        (self.repository / "skills" / "one" / "SKILL.md").write_text("updated")
        self.assertEqual((link / "SKILL.md").read_text(), "updated")
        repeated = self.run_script("one")
        self.assertEqual(repeated.returncode, 0)
        self.assertIn("UNCHANGED", repeated.stdout)
        self.assertFalse((link / "one").exists())

    def test_conflict_prevents_entire_batch(self):
        self.target.mkdir()
        (self.target / "two").mkdir()
        result = self.run_script("--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / "one").exists())
        self.assertFalse((self.target / "two").is_symlink())

    def test_dangling_or_different_link_is_preserved(self):
        self.target.mkdir()
        other = self.root / "missing"
        (self.target / "one").symlink_to(other)
        result = self.run_script("one")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / "one").readlink(), other)

    def test_unknown_or_unselected_skills_do_not_mutate(self):
        for args in [[], ["missing"], ["../one"], ["--all", "one"]]:
            result = self.run_script(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.target.exists())

    def test_all_includes_skills_added_later(self):
        self.assertEqual(self.run_script("--all").returncode, 0)
        folder = self.repository / "skills" / "three"
        folder.mkdir()
        (folder / "SKILL.md").write_text("new")
        self.assertEqual(self.run_script("--all").returncode, 0)
        self.assertTrue((self.target / "three").is_symlink())
        # Removing the installation link preserves the repository skill.
        (self.target / "three").unlink()
        self.assertTrue((folder / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
