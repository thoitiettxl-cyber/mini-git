"""Staging area, tree building, commit creation, checkout.

The staging area is a JSON file (.git/MINIGIT_INDEX) mapping repo-relative
paths to blob shas. This is a deliberate simplification: real git uses a
binary index, but fsck/log/pack/clone interop does not need it.
"""
import json
import os
import time

from .objects import (ObjectStore, encode_tree, decode_tree, encode_commit,
                      decode_commit, BLOB, TREE, COMMIT)
from . import refs

INDEX_NAME = "MINIGIT_INDEX"


def _index_path(repo) -> str:
    return os.path.join(repo.gitdir, INDEX_NAME)


def read_index(repo) -> dict:
    p = _index_path(repo)
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def write_index(repo, index: dict):
    tmp = _index_path(repo) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, _index_path(repo))


def _rel(repo, abspath: str) -> str:
    return os.path.relpath(abspath, repo.workdir).replace(os.sep, "/")


def add(repo, store: ObjectStore, paths) -> dict:
    """Stage files. Returns the new index."""
    index = read_index(repo)
    targets = []
    for p in paths:
        ap = os.path.abspath(os.path.join(repo.workdir, p))
        if os.path.isdir(ap):
            for root, _dirs, files in os.walk(ap):
                for fn in files:
                    targets.append(os.path.join(root, fn))
        elif os.path.isfile(ap):
            targets.append(ap)
        else:
            raise SystemExit(f"pathspec '{p}' did not match any file")
    for ap in targets:
        rel = _rel(repo, ap)
        if rel.startswith(".git/") or rel == ".git":
            continue
        with open(ap, "rb") as f:
            data = f.read()
        sha = store.write(BLOB, data)
        index[rel] = sha
    write_index(repo, index)
    return index


def build_tree(store: ObjectStore, index: dict) -> str:
    """Build tree objects bottom-up from a flat {path: blob_sha} mapping."""

    def rec(sub: dict) -> str:
        entries = []
        children = {}
        for path, sha in sub.items():
            if "/" in path:
                head, rest = path.split("/", 1)
                children.setdefault(head, {})[rest] = sha
            else:
                entries.append((0o100644, path, sha))
        for name, child in children.items():
            entries.append((0o040000, name, rec(child)))
        return store.write(TREE, encode_tree(entries))

    return rec(dict(index))


def tree_to_files(store: ObjectStore, tree_sha: str, prefix="") -> dict:
    """Flatten a tree -> {path: blob_sha}."""
    out = {}

    def rec(sha: str, pre: str):
        for mode, name, child in decode_tree(store.read_bytes(sha, TREE)):
            if mode == 0o040000:
                rec(child, pre + name + "/")
            else:
                out[pre + name] = child

    rec(tree_sha, prefix)
    return out


def _ident(repo) -> str:
    name = os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL")
    if not name or not email:
        name, email = _read_config(repo)
    if not name:
        name = "mini-git"
    if not email:
        email = "mini-git@local"
    now = int(time.time())
    tz = _tz_offset()
    return f"{name} <{email}> {now} {tz}"


def _read_config(repo):
    name = email = None
    cfg = os.path.join(repo.gitdir, "config")
    if os.path.exists(cfg):
        with open(cfg) as f:
            for line in f:
                s = line.strip()
                if s.startswith("name ="):
                    name = s.split("=", 1)[1].strip()
                elif s.startswith("email ="):
                    email = s.split("=", 1)[1].strip()
    return name, email


def _tz_offset() -> str:
    off = -time.timezone if time.localtime().tm_isdst == 0 else -time.altzone
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{sign}{off // 3600:02d}{(off % 3600) // 60:02d}"


def commit(repo, store: ObjectStore, message: str, parents=None,
           author=None, committer=None, index=None) -> str:
    """Create a commit from the staging area. Returns the commit sha."""
    if index is None:
        index = read_index(repo)
    if not index:
        raise SystemExit("nothing to commit (staging area is empty)")
    tree = build_tree(store, index)
    if parents is None:
        head = refs.current_commit(repo, store)
        parents = [head] if head else []
    ident = _ident(repo)
    author = author or ident
    committer = committer or ident
    if not message.endswith("\n"):
        message += "\n"
    sha = store.write(COMMIT, encode_commit(tree, parents, author, committer,
                                            message))
    ref = refs.head_ref(repo)
    if ref:
        refs.update_ref(repo, ref, sha)
    else:
        refs.set_head(repo, sha)  # detached: move HEAD itself
    return sha


def checkout_tree(repo, store: ObjectStore, tree_sha: str):
    """Write a tree into the workdir and refresh the staging area."""
    wanted = tree_to_files(store, tree_sha)
    # remove tracked files that are not in the target tree
    current = read_index(repo)
    for path in sorted(set(current) - set(wanted)):
        ap = os.path.join(repo.workdir, path)
        if os.path.isfile(ap):
            os.remove(ap)
    for path, blob in wanted.items():
        ap = os.path.join(repo.workdir, path)
        os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
        data = store.read_bytes(blob, BLOB)
        with open(ap, "wb") as f:
            f.write(data)
    write_index(repo, wanted)


def checkout(repo, store: ObjectStore, target: str) -> str:
    """Checkout a branch name or commit sha. Returns the new HEAD sha."""
    sha = refs.resolve(repo, target, store)
    if not sha:
        raise SystemExit(f"unknown revision: {target}")
    info = decode_commit(store.read_bytes(sha, COMMIT))
    checkout_tree(repo, store, info["tree"])
    branch_ref = f"refs/heads/{target}"
    if any(n == target for n, _ in refs.list_branches(repo)):
        refs.set_head(repo, branch_ref)
    else:
        refs.set_head(repo, sha)
    return sha
