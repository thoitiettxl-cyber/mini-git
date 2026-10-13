"""Commit-level 3-way merge: merge base, tree merge, fast-forward."""
import os

from .objects import decode_commit, BLOB
from .diff import merge_texts
from .staging import tree_to_files, read_index, write_index, commit as make_commit
from . import refs


def merge_base(store, sha1: str, sha2: str):
    """First common ancestor found by BFS (like git's best guess)."""
    ancestors = set()
    queue = [sha1]
    while queue:
        c = queue.pop(0)
        if c in ancestors:
            continue
        ancestors.add(c)
        try:
            info = decode_commit(store.read_bytes(c, "commit"))
        except Exception:
            continue
        queue.extend(info["parents"])
    queue = [sha2]
    seen = set()
    while queue:
        c = queue.pop(0)
        if c in seen:
            continue
        seen.add(c)
        if c in ancestors:
            return c
        try:
            info = decode_commit(store.read_bytes(c, "commit"))
        except Exception:
            continue
        queue.extend(info["parents"])
    return None


def _text(store, blob_sha):
    if blob_sha is None:
        return None
    return store.read_bytes(blob_sha, BLOB).decode("utf-8", errors="replace")


def merge_commits(repo, store, other_sha: str, message: str = None,
                  branch_name: str = None):
    """Merge other_sha into HEAD. Returns (status, detail).

    status: 'up-to-date' | 'fast-forward' | 'merged' | 'conflicts'
    """
    cur = refs.current_commit(repo, store)
    if cur == other_sha:
        return "up-to-date", cur
    base = merge_base(store, cur, other_sha)
    if base == other_sha:
        return "up-to-date", cur
    if base == cur:
        # fast-forward
        info = decode_commit(store.read_bytes(other_sha, "commit"))
        from .staging import checkout_tree
        checkout_tree(repo, store, info["tree"])
        ref = refs.head_ref(repo)
        if ref:
            refs.update_ref(repo, ref, other_sha)
        else:
            refs.set_head(repo, other_sha)
        return "fast-forward", other_sha
    if base is None:
        raise SystemExit("no common ancestor: cannot merge unrelated histories")
    base_files = tree_to_files(store, decode_commit(
        store.read_bytes(base, "commit"))["tree"])
    cur_files = tree_to_files(store, decode_commit(
        store.read_bytes(cur, "commit"))["tree"])
    oth_files = tree_to_files(store, decode_commit(
        store.read_bytes(other_sha, "commit"))["tree"])
    new_index = {}
    conflicts = []
    for path in sorted(set(base_files) | set(cur_files) | set(oth_files)):
        b, c, o = base_files.get(path), cur_files.get(path), oth_files.get(path)
        if c == o:
            if c is not None:
                new_index[path] = c
            continue
        if b == c:
            if o is not None:
                new_index[path] = o
            continue
        if b == o:
            if c is not None:
                new_index[path] = c
            continue
        # true 3-way needed
        bt, ct, ot = _text(store, b), _text(store, c), _text(store, o)
        if ct is None or ot is None:
            # delete/modify conflict (or both deleted handled above)
            conflicts.append(path)
            merged = (f"<<<<<<< HEAD\n{ct or ''}"
                      f"=======\n{ot or ''}>>>>>>> {other_sha[:7]}\n")
        else:
            merged, had_conflict = merge_texts(
                bt or "", ct, ot, left_label="HEAD",
                right_label=branch_name or other_sha[:7])
            if had_conflict:
                conflicts.append(path)
        ap = os.path.join(repo.workdir, path)
        os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            f.write(merged)
        if path not in conflicts:
            new_index[path] = store.write(BLOB, merged.encode("utf-8"))
    # stage clean results; conflicted files stay in workdir with markers
    staged = {}
    for path, blob in new_index.items():
        if path not in conflicts:
            staged[path] = blob
    write_index(repo, staged)
    if conflicts:
        return "conflicts", conflicts
    if message is None:
        message = f"Merge commit {other_sha[:7]} into {cur[:7]}\n"
    new_sha = make_commit(repo, store, message, parents=[cur, other_sha],
                          index=staged)
    return "merged", new_sha
