"""Rebase: replay a linear commit sequence onto a new upstream tip."""
import os

from .objects import decode_commit, BLOB, COMMIT
from .diff import merge_texts
from .merge import merge_base
from .staging import (tree_to_files, read_index, write_index, commit as
                      make_commit, checkout_tree)
from . import refs


def _linear_range(store, base: str, tip: str):
    """Commits from base (exclusive) to tip, oldest-first, first-parent walk."""
    out = []
    c = tip
    while c and c != base:
        out.append(c)
        info = decode_commit(store.read_bytes(c, COMMIT))
        c = info["parents"][0] if info["parents"] else None
    out.reverse()
    return out


def rebase(repo, store, upstream: str):
    """Rebase current branch onto upstream. Returns (status, detail)."""
    branch = refs.head_ref(repo)
    if not branch:
        raise SystemExit("cannot rebase a detached HEAD")
    cur = refs.current_commit(repo, store)
    up = refs.resolve(repo, upstream, store)
    if not up:
        raise SystemExit(f"unknown revision: {upstream}")
    base = merge_base(store, up, cur)
    if base == up and base == cur:
        return "up-to-date", cur
    if base == cur:
        # upstream is ahead: fast-forward
        refs.update_ref(repo, branch, up)
        up_info = decode_commit(store.read_bytes(up, COMMIT))
        checkout_tree(repo, store, up_info["tree"])
        return "fast-forward", up
    todo = _linear_range(store, base, cur)
    if not todo:
        return "up-to-date", cur
    # move branch to upstream and check out its tree
    refs.update_ref(repo, branch, up)
    up_info = decode_commit(store.read_bytes(up, COMMIT))
    checkout_tree(repo, store, up_info["tree"])
    new_tip = up
    for c in todo:
        info = decode_commit(store.read_bytes(c, COMMIT))
        parent = info["parents"][0] if info["parents"] else None
        old_files = tree_to_files(
            store, decode_commit(store.read_bytes(parent, COMMIT))["tree"]) \
            if parent else {}
        new_files = tree_to_files(store, info["tree"])
        index = read_index(repo)
        for path in sorted(set(old_files) | set(new_files)):
            old_b, new_b = old_files.get(path), new_files.get(path)
            if old_b == new_b:
                continue
            ap = os.path.join(repo.workdir, path)
            cur_data = None
            if os.path.isfile(ap):
                with open(ap, "rb") as f:
                    cur_data = f.read()
            old_data = store.read_bytes(old_b, BLOB) if old_b else None
            new_data = store.read_bytes(new_b, BLOB) if new_b else None
            if cur_data != old_data:
                # workdir changed since upstream: 3-way per file
                if old_data is None or cur_data is None or new_data is None:
                    raise SystemExit(
                        f"rebase conflict at {path}: delete/modify clash; "
                        f"aborting (branch left at {new_tip[:7]})")
                merged, conflicted = merge_texts(
                    old_data.decode("utf-8", errors="replace"),
                    cur_data.decode("utf-8", errors="replace"),
                    new_data.decode("utf-8", errors="replace"),
                    left_label="upstream", right_label=c[:7])
                if conflicted:
                    raise SystemExit(
                        f"rebase conflict at {path}; aborting "
                        f"(branch left at {new_tip[:7]})")
                new_data = merged.encode("utf-8")
            if new_data is None:
                if os.path.isfile(ap):
                    os.remove(ap)
                index.pop(path, None)
            else:
                os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
                with open(ap, "wb") as f:
                    f.write(new_data)
                index[path] = store.write(BLOB, new_data)
        write_index(repo, index)
        new_tip = make_commit(repo, store, info["message"], parents=[new_tip],
                              author=info.get("author"), index=index)
    return "rebased", new_tip
