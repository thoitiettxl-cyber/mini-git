"""Interop: repos built by mini-git must satisfy the REAL git binary.

- `git fsck --full` accepts the repo
- real git reads commits (`git log`, `git cat-file`)
- real git reads our packs (`git verify-pack -v`)
- `git clone` works and trees match
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def mg(*args, cwd):
    env = dict(os.environ, PYTHONPATH=REPO_ROOT,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t")
    return subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd,
                          env=env, capture_output=True, text=True)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)


def w(cwd, rel, text):
    ap = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
    with open(ap, "w") as f:
        f.write(text)


class InteropBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mg-interop-")
        r = mg("init", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit_file(self, rel, text, msg):
        w(self.tmp, rel, text)
        r = mg("add", rel, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = mg("commit", "-m", msg, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)

    def assertFsckClean(self):
        r = git("fsck", "--full", cwd=self.tmp)
        self.assertEqual(r.returncode, 0,
                         f"fsck failed: {r.stdout}\n{r.stderr}")


class TestBasicInterop(InteropBase):
    def test_fsck_and_log(self):
        self.commit_file("a.txt", "hello\n", "first")
        self.commit_file("sub/b.txt", "world\n" * 10, "second")
        self.assertFsckClean()
        r = git("log", "--format=%H %s", cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("second"))
        # real git reads our commit object
        sha = lines[0].split()[0]
        r = git("cat-file", "-p", sha, cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertIn("second", r.stdout)
        # and our tool reads it too (cross-check)
        r = mg("cat-file", "-p", sha, cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertIn("second", r.stdout)

    def test_branch_and_checkout_interop(self):
        self.commit_file("a.txt", "v1\n", "c1")
        self.assertEqual(mg("branch", "feat", cwd=self.tmp).returncode, 0)
        self.assertEqual(
            mg("checkout", "feat", cwd=self.tmp).returncode, 0)
        self.commit_file("a.txt", "v2\n", "c2")
        self.assertFsckClean()
        r = git("rev-parse", "feat", cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        r = git("log", "--oneline", "feat", cwd=self.tmp)
        self.assertIn("c2", r.stdout)


class TestPackInterop(InteropBase):
    def test_gc_then_real_git_reads_pack(self):
        # several similar files -> real deltas in the pack
        for i in range(6):
            body = "line\n" * 50 + f"unique {i}\n"
            self.commit_file(f"f{i}.txt", body, f"commit {i}")
        r = mg("gc", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        idxs = glob.glob(os.path.join(self.tmp, ".git", "objects", "pack",
                                      "*.idx"))
        self.assertTrue(idxs, "no pack index written")
        for idx in idxs:
            r = git("verify-pack", "-v", idx, cwd=self.tmp)
            self.assertEqual(r.returncode, 0,
                             f"verify-pack failed: {r.stderr}")
            # delta objects appear as resolved types with extra columns
            # (depth + base sha); the summary must report a delta chain
            self.assertIn("chain length = 1", r.stdout,
                          "expected delta objects in pack")
        self.assertFsckClean()
        # real git reads a packed blob
        r = git("rev-parse", "HEAD^{tree}", cwd=self.tmp)
        tree = r.stdout.strip()
        r = git("ls-tree", tree, cwd=self.tmp)
        blob = [l.split()[2] for l in r.stdout.splitlines()
                if l.endswith("f5.txt")][0]
        r = git("cat-file", "-p", blob, cwd=self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertIn("unique 5", r.stdout)
        # our reader also resolves the packed (delta) objects
        r = mg("verify-pack", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("packed objects", r.stdout)

    def test_clone(self):
        self.commit_file("a.txt", "clone me\n", "c1")
        self.commit_file("b.txt", "me too\n", "c2")
        mg("gc", cwd=self.tmp)
        dest = self.tmp + "-clone"
        r = subprocess.run(["git", "clone", "-q", self.tmp, dest],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            r = git("ls-tree", "-r", "HEAD", cwd=dest)
            names = sorted(l.split("\t")[1] for l in r.stdout.splitlines())
            self.assertEqual(names, ["a.txt", "b.txt"])
            with open(os.path.join(dest, "a.txt")) as f:
                self.assertEqual(f.read(), "clone me\n")
            r = git("fsck", "--full", cwd=dest)
            self.assertEqual(r.returncode, 0)
        finally:
            shutil.rmtree(dest, ignore_errors=True)


class TestMergeRebaseInterop(InteropBase):
    def test_merge_commit_interop(self):
        self.commit_file("a.txt", "base\n", "base")
        mg("branch", "feat", cwd=self.tmp)
        mg("checkout", "feat", cwd=self.tmp)
        self.commit_file("feat.txt", "feature work\n", "feat commit")
        mg("checkout", "main", cwd=self.tmp)
        self.commit_file("main.txt", "main work\n", "main commit")
        r = mg("merge", "feat", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("Merge made", r.stdout)
        self.assertFsckClean()
        r = git("log", "--merges", "--format=%s", cwd=self.tmp)
        self.assertIn("Merge", r.stdout)
        # merged tree has all files per real git
        r = git("ls-tree", "-r", "--name-only", "HEAD", cwd=self.tmp)
        self.assertEqual(sorted(r.stdout.split()),
                         ["a.txt", "feat.txt", "main.txt"])

    def test_merge_conflict_markers(self):
        self.commit_file("a.txt", "line1\nline2\nline3\n", "base")
        mg("branch", "feat", cwd=self.tmp)
        mg("checkout", "feat", cwd=self.tmp)
        self.commit_file("a.txt", "line1\nFEAT\nline3\n", "feat change")
        mg("checkout", "main", cwd=self.tmp)
        self.commit_file("a.txt", "line1\nMAIN\nline3\n", "main change")
        r = mg("merge", "feat", cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0)  # conflicts -> exit 1
        with open(os.path.join(self.tmp, "a.txt")) as f:
            content = f.read()
        self.assertIn("<<<<<<<", content)
        self.assertIn("=======", content)
        self.assertIn(">>>>>>>", content)

    def test_rebase_interop(self):
        self.commit_file("a.txt", "v1\n", "c1")
        mg("branch", "feat", cwd=self.tmp)
        self.commit_file("b.txt", "main2\n", "c2 main")
        mg("checkout", "feat", cwd=self.tmp)
        self.commit_file("c.txt", "feat1\n", "c3 feat")
        self.commit_file("d.txt", "feat2\n", "c4 feat")
        r = mg("rebase", "main", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFsckClean()
        r = git("log", "--format=%s", cwd=self.tmp)
        self.assertEqual(r.stdout.splitlines(),
                         ["c4 feat", "c3 feat", "c2 main", "c1"])


if __name__ == "__main__":
    unittest.main()
