"""Delta encode/decode and pack v2 write/read roundtrips."""
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minigit.objects import ObjectStore, encode_tree
from minigit.pack import (create_delta, apply_delta, PackWriter, PackReader,
                          encode_ofs_offset, decode_ofs_offset,
                          encode_varint, decode_varint)
from minigit.refs import Repo


class TestDelta(unittest.TestCase):
    def test_roundtrip_random(self):
        rnd = random.Random(42)
        for _ in range(60):
            base = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 3000)))
            # target: mutate base a bit
            tgt = bytearray(base)
            for _ in range(rnd.randrange(0, 20)):
                if tgt and rnd.random() < 0.5:
                    tgt[rnd.randrange(len(tgt))] = rnd.randrange(256)
                else:
                    tgt[rnd.randrange(len(tgt) + 1):rnd.randrange(len(tgt) + 1)] = \
                        bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 30)))
            tgt = bytes(tgt)
            delta = create_delta(base, tgt)
            self.assertEqual(apply_delta(base, delta), tgt)

    def test_edge_cases(self):
        cases = [(b"", b""), (b"", b"abc"), (b"abc", b""),
                 (b"a" * 1000, b"a" * 1000),
                 (b"abcdefgh", b"xyzabcdefgh123"),
                 (os.urandom(500), os.urandom(500))]
        for base, tgt in cases:
            self.assertEqual(apply_delta(base, create_delta(base, tgt)), tgt,
                             f"base={base[:10]} tgt={tgt[:10]}")

    def test_varint_roundtrip(self):
        for n in [0, 1, 127, 128, 255, 16384, 2 ** 32 - 1]:
            v, pos = decode_varint(encode_varint(n), 0)
            self.assertEqual((v, pos), (n, len(encode_varint(n))))

    def test_ofs_offset_roundtrip(self):
        for n in [0, 1, 127, 128, 1000, 2 ** 20, 2 ** 31]:
            v, pos = decode_ofs_offset(encode_ofs_offset(n), 0)
            self.assertEqual((v, pos), (n, len(encode_ofs_offset(n))))


class TestPackRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Repo.init(self.tmp)
        self.store = ObjectStore(self.repo.gitdir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_all_types(self):
        blobs = [self.store.write("blob", f"content {i}\n".encode() * (i + 1))
                  for i in range(5)]
        tree = self.store.write(
            "tree", encode_tree([(0o100644, f"f{i}", b) for i, b in enumerate(blobs)]))
        from minigit.objects import encode_commit
        c1 = self.store.write("commit", encode_commit(
            tree, [], "A <a@x> 1 +0000", "A <a@x> 1 +0000", "one\n"))
        c2 = self.store.write("commit", encode_commit(
            tree, [c1], "A <a@x> 2 +0000", "A <a@x> 2 +0000", "two\n"))
        # make similar blobs so deltas actually trigger
        sims = []
        base = b"line\n" * 200
        for i in range(6):
            data = base + f"tail {i}\n".encode()
            sims.append(self.store.write("blob", data))
        writer = PackWriter()
        expected = {}
        for sha in blobs + [tree, c1, c2] + sims:
            typ, data = self.store.read(sha)
            expected[sha] = (typ, data)
            writer.add_object(sha, typ, data)
        pack_path = os.path.join(self.repo.gitdir, "objects", "pack",
                                 "pack-test.pack")
        writer.write(pack_path)
        reader = PackReader(pack_path)
        try:
            self.assertEqual(set(reader.all_shas()), set(expected))
            for sha, (typ, data) in expected.items():
                self.assertEqual(reader.read_object(sha), (typ, data))
        finally:
            reader.close()

    def test_delta_chain_resolution(self):
        # blobs where each is a small edit of the previous -> delta chains
        prev = None
        shas = []
        data = b"start\n" * 100
        for i in range(8):
            data = data + f"extra line {i}\n".encode()
            shas.append(self.store.write("blob", data))
        writer = PackWriter()
        for sha in shas:
            typ, d = self.store.read(sha)
            writer.add_object(sha, typ, d)
        pack_path = os.path.join(self.repo.gitdir, "objects", "pack",
                                 "pack-chain.pack")
        writer.write(pack_path)
        reader = PackReader(pack_path)
        try:
            for sha in shas:
                typ, d = self.store.read(sha)
                self.assertEqual(reader.read_object(sha), (typ, d))
        finally:
            reader.close()

    def test_store_reads_through_pack(self):
        sha = self.store.write("blob", b"packed hello\n")
        writer = PackWriter()
        typ, data = self.store.read(sha)
        writer.add_object(sha, typ, data)
        pack_path = os.path.join(self.repo.gitdir, "objects", "pack",
                                 "pack-thru.pack")
        writer.write(pack_path)
        for r in self.store.packs:
            r.close()
        os.remove(os.path.join(self.repo.gitdir, "objects", sha[:2], sha[2:]))
        self.store.refresh_packs()
        rtyp, rdata = self.store.read(sha)
        self.assertEqual((rtyp, rdata), (typ, data))


if __name__ == "__main__":
    unittest.main()
