"""Myers diff vs difflib oracle, unified diff, diff3/merge."""
import difflib
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minigit.diff import opcodes, unified_diff, diff3_merge, merge_texts


def _counts(ops):
    # (deletions, insertions); both are uniquely determined by a minimal diff
    d = i = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "delete":
            d += i2 - i1
        elif tag == "insert":
            i += j2 - j1
        elif tag == "replace":
            d += i2 - i1
            i += j2 - j1
    return d, i


class TestMyers(unittest.TestCase):
    def test_matches_difflib(self):
        rnd = random.Random(7)
        alpha = ["a", "b", "c", "d", "e"]
        for _ in range(200):
            a = [rnd.choice(alpha) for _ in range(rnd.randrange(0, 12))]
            b = [rnd.choice(alpha) for _ in range(rnd.randrange(0, 12))]
            mine = opcodes(a, b)
            theirs = difflib.SequenceMatcher(None, a, b).get_opcodes()
            # our script must be valid and minimal: difflib is only a
            # heuristic, so ours must be no worse, with d-i == len(a)-len(b)
            self.assertEqual(_apply(a, b, mine), b)
            md, mi = _counts(mine)
            td, ti = _counts(theirs)
            self.assertEqual(md - mi, len(a) - len(b))
            self.assertLessEqual(md, td)
            self.assertLessEqual(mi, ti)

    def test_empty(self):
        self.assertEqual(opcodes([], []), [])
        self.assertEqual(opcodes([], ["x"]), [("insert", 0, 0, 0, 1)])
        self.assertEqual(opcodes(["x"], []), [("delete", 0, 1, 0, 0)])


def _apply(a, b, ops):
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(a[i1:i2])
        elif tag == "delete":
            pass
        elif tag == "insert":
            out.extend(b[j1:j2])
        elif tag == "replace":
            out.extend(b[j1:j2])
    return out


class TestUnifiedDiff(unittest.TestCase):
    def test_basic(self):
        a = ["one\n", "two\n", "three\n"]
        b = ["one\n", "TWO\n", "three\n"]
        d = unified_diff(a, b, "a/f", "b/f")
        self.assertIn("--- a/f", d)
        self.assertIn("+++ b/f", d)
        self.assertIn("-two", d)
        self.assertIn("+TWO", d)
        self.assertIn("@@", d)

    def test_no_changes(self):
        self.assertEqual(unified_diff(["a\n"], ["a\n"]), "")

    def test_hunk_context(self):
        a = [f"l{i}\n" for i in range(20)]
        b = list(a)
        b[10] = "CHANGED\n"
        d = unified_diff(a, b)
        # context of 3 lines around line 10
        self.assertIn("l7", d)
        self.assertIn("l13", d)
        self.assertNotIn("l0\n", d.replace("l10", ""))


class TestDiff3(unittest.TestCase):
    def test_clean_merge_both_sides(self):
        base = "a\nb\nc\nd\n"
        left = "a\nB\nc\nd\n"
        right = "a\nb\nc\nD\n"
        merged, conflict = merge_texts(base, left, right)
        self.assertFalse(conflict)
        self.assertEqual(merged, "a\nB\nc\nD\n")

    def test_same_change_both_sides(self):
        base = "a\nb\n"
        left = right = "a\nB\n"
        merged, conflict = merge_texts(base, left, right)
        self.assertFalse(conflict)
        self.assertEqual(merged, "a\nB\n")

    def test_one_side_unchanged(self):
        base = "a\nb\nc\n"
        left = "a\nB\nc\n"
        merged, conflict = merge_texts(base, left, base)
        self.assertFalse(conflict)
        self.assertEqual(merged, left)

    def test_conflict_markers(self):
        base = "a\nb\nc\n"
        left = "a\nLEFT\nc\n"
        right = "a\nRIGHT\nc\n"
        merged, conflict = merge_texts(base, left, right,
                                       left_label="L", right_label="R")
        self.assertTrue(conflict)
        self.assertIn("<<<<<<< L", merged)
        self.assertIn("=======", merged)
        self.assertIn(">>>>>>> R", merged)
        self.assertIn("LEFT", merged)
        self.assertIn("RIGHT", merged)

    def test_insert_both_sides_different_places(self):
        base = "a\nd\n"
        left = "a\nb\nc\nd\n"
        right = "a\nd\ne\n"
        merged, conflict = merge_texts(base, left, right)
        self.assertFalse(conflict)
        self.assertEqual(merged, "a\nb\nc\nd\ne\n")

    def test_delete_vs_modify_conflict(self):
        base = "a\nb\nc\n"
        left = "a\nc\n"          # deleted b
        right = "a\nB\nc\n"       # modified b
        _merged, conflict = merge_texts(base, left, right)
        self.assertTrue(conflict)

    def test_chunks_structure(self):
        chunks = diff3_merge(["a\n"], ["b\n"], ["c\n"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "conflict")


if __name__ == "__main__":
    unittest.main()
