"""Object store: SHA-1 compatibility with real git, tree/commit roundtrips."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minigit.objects import (hash_object, encode_tree, decode_tree,
                             encode_commit, decode_commit, ObjectStore,
                             BLOB, TREE, COMMIT)
from minigit.refs import Repo


def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=cwd, check=True).stdout.strip()


class TestHashCompatibility(unittest.TestCase):
    def test_blob_sha_matches_real_git(self):
        for data in [b"hello\n", b"", os.urandom(500), b"x" * 70000]:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(data)
                path = f.name
            try:
                expected = git("hash-object", path)
                self.assertEqual(hash_object("blob", data), expected)
            finally:
                os.unlink(path)

    def test_tree_sha_matches_real_git(self):
        tmp = tempfile.mkdtemp()
        try:
            git("init", "-q", cwd=tmp)
            blobs = {}
            for name, data in [("a.txt", b"aaa\n"), ("b.txt", b"b" * 100),
                               ("sub/c.txt", b"ccc\n")]:
                p = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
                blobs[name] = (git("hash-object", "-w", p, cwd=tmp), data)
            # sub tree via git mktree
            sub_in = f"100644 blob {blobs['sub/c.txt'][0]}\tc.txt\n"
            sub_tree = git("mktree", cwd=tmp, input=sub_in) if False else \
                subprocess.run(["git", "mktree"], input=sub_in, cwd=tmp,
                               capture_output=True, text=True,
                               check=True).stdout.strip()
            top_in = (f"100644 blob {blobs['a.txt'][0]}\ta.txt\n"
                      f"100644 blob {blobs['b.txt'][0]}\tb.txt\n"
                      f"040000 tree {sub_tree}\tsub\n")
            expected_top = subprocess.run(
                ["git", "mktree"], input=top_in, cwd=tmp, capture_output=True,
                text=True, check=True).stdout.strip()
            entries = [(0o100644, "a.txt", blobs["a.txt"][0]),
                       (0o100644, "b.txt", blobs["b.txt"][0]),
                       (0o040000, "sub", sub_tree)]
            self.assertEqual(hash_object("tree", encode_tree(entries)),
                             expected_top)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_commit_sha_matches_real_git(self):
        tmp = tempfile.mkdtemp()
        try:
            git("init", "-q", cwd=tmp)
            git("config", "user.email", "t@t", cwd=tmp)
            git("config", "user.name", "t", cwd=tmp)
            with open(os.path.join(tmp, "f"), "w") as fh:
                fh.write("x\n")
            git("add", "f", cwd=tmp)
            git("commit", "-qm", "msg", cwd=tmp)
            expected = git("rev-parse", "HEAD", cwd=tmp)
            raw = git("cat-file", "commit", "HEAD", cwd=tmp)
            # re-encode the exact commit body git produced
            info = decode_commit(raw.encode())
            # rebuild with same fields (author/committer lines preserved)
            body = raw.split("\n\n", 1)[1]
            head = raw.split("\n\n", 1)[0]
            rebuilt = (head + "\n\n" + body).encode()
            if not rebuilt.endswith(b"\n"):
                rebuilt += b"\n"
            self.assertEqual(hash_object("commit", rebuilt), expected)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestLooseStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Repo.init(self.tmp)
        self.store = ObjectStore(self.repo.gitdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_roundtrip(self):
        for typ, data in [("blob", b"data\n"),
                          ("tree", encode_tree([(0o100644, "a",
                                                  "ab" * 20)])),
                          ("commit", b"tree " + b"ab" * 20 + b"\n\nmsg\n")]:
            sha = self.store.write(typ, data)
            rtyp, rdata = self.store.read(sha)
            self.assertEqual((rtyp, rdata), (typ, data))
            # on-disk format check: zlib("type size\\0data")
            path = os.path.join(self.repo.gitdir, "objects",
                                sha[:2], sha[2:])
            raw = zlib.decompress(open(path, "rb").read())
            self.assertEqual(raw, f"{typ} {len(data)}\0".encode() + data)

    def test_tree_encode_decode(self):
        entries = [(0o100644, "z.txt", "ab" * 20),
                   (0o100644, "a.txt", "cd" * 20),
                   (0o040000, "dir", "ef" * 20)]
        decoded = decode_tree(encode_tree(entries))
        self.assertEqual(sorted(decoded), sorted(entries))
        # git sorts by name
        names = [n for _, n, _ in decode_tree(encode_tree(entries))]
        self.assertEqual(names, sorted(names))

    def test_commit_encode_decode(self):
        data = encode_commit("ab" * 20, ["cd" * 20], "A <a@x> 1 +0000",
                             "A <a@x> 1 +0000", "hello\n")
        info = decode_commit(data)
        self.assertEqual(info["tree"], "ab" * 20)
        self.assertEqual(info["parents"], ["cd" * 20])
        self.assertEqual(info["message"], "hello\n")

    def test_missing_object_raises(self):
        with self.assertRaises(KeyError):
            self.store.read("00" * 20)


if __name__ == "__main__":
    unittest.main()
