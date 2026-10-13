"""Rebase: linear replay onto a new upstream, via the staging API."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minigit.objects import ObjectStore, decode_commit, COMMIT
from minigit.refs import Repo, resolve, head_ref
from minigit.staging import add, commit, checkout, tree_to_files
from minigit.rebase import rebase
from minigit.merge import merge_base


def write_file(repo, rel, text):
    ap = os.path.join(repo.workdir, rel)
    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
    with open(ap, "w") as f:
        f.write(text)


def read_file(repo, rel):
    with open(os.path.join(repo.workdir, rel)) as f:
        return f.read()


class TestRebase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Repo.init(self.tmp)
        self.store = ObjectStore(self.repo.gitdir)
        write_file(self.repo, "a.txt", "a1\n")
        add(self.repo, self.store, ["a.txt"])
        self.c1 = commit(self.repo, self.store, "c1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tree_of(self, sha):
        info = decode_commit(self.store.read_bytes(sha, COMMIT))
        return tree_to_files(self.store, info["tree"])

    def test_basic_rebase(self):
        # main: c1 -> c2 (adds b.txt). feature branches at c1, adds c.txt.
        write_file(self.repo, "b.txt", "b\n")
        add(self.repo, self.store, ["b.txt"])
        c2 = commit(self.repo, self.store, "c2 on main")
        checkout(self.repo, self.store, self.c1)
        from minigit import refs
        refs.update_ref(self.repo, "refs/heads/feature", self.c1)
        checkout(self.repo, self.store, "feature")
        write_file(self.repo, "c.txt", "c\n")
        add(self.repo, self.store, ["c.txt"])
        c3 = commit(self.repo, self.store, "c3 on feature")
        status, new_tip = rebase(self.repo, self.store, "main")
        self.assertEqual(status, "rebased")
        # new history: c1 -> c2 -> c3'
        info = decode_commit(self.store.read_bytes(new_tip, COMMIT))
        parent = decode_commit(self.store.read_bytes(info["parents"][0], COMMIT))
        self.assertEqual(info["parents"][0], c2)
        self.assertEqual(info["message"], "c3 on feature\n")
        self.assertNotEqual(new_tip, c3)  # sha rewritten
        files = self._tree_of(new_tip)
        self.assertEqual(set(files), {"a.txt", "b.txt", "c.txt"})
        self.assertEqual(read_file(self.repo, "c.txt"), "c\n")

    def test_rebase_fast_forward(self):
        write_file(self.repo, "b.txt", "b\n")
        add(self.repo, self.store, ["b.txt"])
        c2 = commit(self.repo, self.store, "c2")
        from minigit import refs
        refs.update_ref(self.repo, "refs/heads/old", self.c1)
        checkout(self.repo, self.store, "old")
        status, tip = rebase(self.repo, self.store, "main")
        self.assertEqual(status, "fast-forward")
        self.assertEqual(tip, c2)

    def test_rebase_up_to_date(self):
        status, tip = rebase(self.repo, self.store, "main")
        self.assertEqual(status, "up-to-date")

    def test_rebase_multiple_commits(self):
        write_file(self.repo, "m.txt", "m1\n")
        add(self.repo, self.store, ["m.txt"])
        c2 = commit(self.repo, self.store, "main c2")
        checkout(self.repo, self.store, self.c1)
        from minigit import refs
        refs.update_ref(self.repo, "refs/heads/feat", self.c1)
        checkout(self.repo, self.store, "feat")
        for i, msg in enumerate(["f1", "f2", "f3"]):
            write_file(self.repo, f"f{i}.txt", f"{msg}\n")
            add(self.repo, self.store, [f"f{i}.txt"])
            commit(self.repo, self.store, msg)
        status, new_tip = rebase(self.repo, self.store, "main")
        self.assertEqual(status, "rebased")
        # walk 3 commits back to c2
        c = new_tip
        msgs = []
        for _ in range(3):
            info = decode_commit(self.store.read_bytes(c, COMMIT))
            msgs.append(info["message"].strip())
            c = info["parents"][0]
        self.assertEqual(msgs, ["f3", "f2", "f1"])
        self.assertEqual(c, c2)


if __name__ == "__main__":
    unittest.main()
