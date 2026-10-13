# mini-git

A from-scratch reimplementation of git's core, written in Python 3 using
**only the standard library** (`hashlib`, `zlib`, `argparse`, …). No git
libraries (no GitPython, dulwich, pygit2) are used anywhere in the core.

Repositories created by mini-git are byte-compatible with real git:
`git fsck --full` accepts them, and real git can read their commits,
trees and packfiles (including delta-compressed objects).

## Features

- **Object store** — `blob` / `tree` / `commit` objects in the exact loose
  format git uses (`zlib("<type> <size>\\0<content>")`, SHA-1 addressing).
- **Packfiles** — pack v2 writer with **delta compression** (git delta
  format, `OFS_DELTA`), pack index v2 writer, and a reader that resolves
  both `OFS_DELTA` and `REF_DELTA` (including delta chains).
- **Diff** — Myers O(ND) diff with unified-diff output.
- **diff3 / 3-way merge** — file-level 3-way merge (`merge-file` style with
  `<<<<<<<` / `=======` / `>>>>>>>` conflict markers) and branch merges
  (fast-forward or true merge commits).
- **Rebase** — replay a linear commit sequence onto a new upstream tip,
  preserving messages/authors and rewriting SHAs like git does.
- **CLI** — `init`, `hash-object`, `cat-file`, `add`, `commit`, `log`,
  `branch`, `checkout`, `status`, `diff`, `gc`, `merge`, `merge-file`,
  `rebase`, `verify-pack`.

## Requirements

- Python 3.8+ (developed on 3.13), no third-party packages.
- A real `git` binary is needed only for the interop tests.

## Running the tests

```sh
python3 -m unittest discover -s tests
```

The suite has 37 tests:

- **Unit tests** — SHA-1 compatibility of blob/tree/commit encoding against
  `git hash-object` / `git mktree` / `git cat-file`; tree & commit
  roundtrips; delta encode/decode (seeded random + edge cases); varint
  roundtrips; pack write→read roundtrips incl. delta chains; Myers diff
  cross-checked against `difflib` (validity + minimality); unified diff;
  diff3 merge cases (clean, identical changes, conflicts, delete/modify);
  rebase (basic, fast-forward, multi-commit).
- **Interop tests** (shell out to the real git binary) — build a repo with
  the mini-git CLI, then assert `git fsck --full` passes, `git log` /
  `git cat-file` read our commits, `git verify-pack -v` accepts our packs
  (and reports delta chains), `git clone` reproduces the tree, and merge /
  rebase results stay fsck-clean.

## Usage

```sh
# run from the repo root; or pip-install / put it on PYTHONPATH
python3 -m minigit init myrepo
cd myrepo
echo hello > a.txt
python3 -m minigit add a.txt
python3 -m minigit commit -m "first commit"
python3 -m minigit log --oneline

# pack loose objects (with deltas) into .git/objects/pack/
python3 -m minigit gc

# verify with REAL git:
git fsck --full          # must report no errors
git log --oneline
git verify-pack -v .git/objects/pack/*.idx

# branching, merging, rebasing
python3 -m minigit branch feature
python3 -m minigit checkout feature
# ... edit, add, commit ...
python3 -m minigit checkout main
python3 -m minigit merge feature
python3 -m minigit rebase main   # on the feature branch

# 3-way file merge (like git merge-file)
python3 -m minigit merge-file current.txt base.txt other.txt
```

## Architecture

```
minigit/
  objects.py   loose object read/write, tree & commit codecs
  refs.py      repo discovery, HEAD / refs handling
  staging.py   staging area (JSON index), tree building, commit, checkout
  pack.py      delta codec, pack v2 + index v2 writer, pack reader
  diff.py      Myers diff, unified diff, diff3 merge
  merge.py     merge-base + commit-level 3-way merge
  rebase.py    linear history replay onto a new upstream
  cli.py       argparse CLI
```

Key format notes:

- Loose objects are byte-identical to git's: `zlib("blob 12\\0hello world\\n")`.
- Pack writer emits v2 packs; similar blobs become `OFS_DELTA` objects
  using git's delta instruction set (copy/insert ops). The index v2
  carries fanout table, sorted SHAs, CRC32s and offsets.
- The delta builder uses a greedy longest-match search with a 4-byte hash
  index; the reader resolves delta chains recursively.

## Limitations (by design)

- The staging area is a JSON file (`.git/MINIGIT_INDEX`), not git's binary
  index — `git status` in real git will show files as untracked, but
  `fsck` / `log` / `clone` interop is unaffected.
- All files are stored with mode `100644` (no executable bit tracking).
- No networking, no remotes, no tags CLI, no interactive conflict editing.
- Author identity comes from `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`,
  `.git/config`, or defaults to `mini-git <mini-git@local>`.

## License

MIT.
