"""mini-git command line interface."""
import argparse
import hashlib
import os
import sys
import time

from . import refs
from .refs import Repo
from .objects import ObjectStore, decode_commit, BLOB, TREE, COMMIT
from .staging import (read_index, add as stage_add, commit as make_commit,
                      tree_to_files, checkout as do_checkout)
from .diff import unified_diff
from .merge import merge_commits
from .rebase import rebase as do_rebase


def _repo_and_store(args):
    repo = Repo.discover()
    store = ObjectStore(repo.gitdir)
    return repo, store


def cmd_init(args):
    repo = Repo.init(args.dir or ".")
    print(f"Initialized empty mini-git repository in {repo.gitdir}")


def cmd_hash_object(args):
    repo, store = _repo_and_store(args)
    with open(args.file, "rb") as f:
        data = f.read()
    typ = args.type
    from .objects import hash_object
    sha = hash_object(typ, data)
    if args.write:
        store.write(typ, data)
    print(sha)


def cmd_cat_file(args):
    repo, store = _repo_and_store(args)
    sha = refs.resolve(repo, args.object, store)
    if not sha:
        sys.exit(f"bad object: {args.object}")
    typ, data = store.read(sha)
    if args.t:
        print(typ)
    elif args.s:
        print(len(data))
    else:
        sys.stdout.buffer.write(data)


def cmd_add(args):
    repo, store = _repo_and_store(args)
    stage_add(repo, store, args.paths)


def cmd_commit(args):
    repo, store = _repo_and_store(args)
    sha = make_commit(repo, store, args.message)
    ref = refs.head_ref(repo)
    print(f"[{(ref or 'detached').split('/')[-1]} {sha[:7]}] {args.message.splitlines()[0]}")


def _fmt_date(ts_tz: str) -> str:
    parts = ts_tz.rsplit(" ", 2)
    try:
        ts = int(parts[-2])
    except (ValueError, IndexError):
        return ts_tz
    tz = parts[-1] if len(parts) == 3 else "+0000"
    tm = time.gmtime(ts)
    return time.strftime("%a %b %d %H:%M:%S %Y", tm) + f" {tz}"


def cmd_log(args):
    repo, store = _repo_and_store(args)
    rev = args.rev or "HEAD"
    sha = refs.resolve(repo, rev, store)
    if not sha:
        sys.exit(f"unknown revision: {rev}")
    count = 0
    while sha:
        info = decode_commit(store.read_bytes(sha, COMMIT))
        if args.oneline:
            print(f"{sha[:7]} {info['message'].splitlines()[0]}")
        else:
            print(f"commit {sha}")
            if len(info["parents"]) > 1:
                print("Merge: " + " ".join(p[:7] for p in info["parents"]))
            print(f"Author: {info.get('author', '').rsplit(' ', 2)[0]}")
            print(f"Date:   {_fmt_date(info.get('author', ''))}")
            print()
            for line in info["message"].splitlines():
                print(f"    {line}")
            print()
        count += 1
        if args.max_count and count >= args.max_count:
            break
        sha = info["parents"][0] if info["parents"] else None


def cmd_branch(args):
    repo, store = _repo_and_store(args)
    if args.delete:
        path = os.path.join(repo.gitdir, "refs", "heads", args.delete)
        if os.path.exists(path):
            os.remove(path)
            print(f"Deleted branch {args.delete}.")
        else:
            sys.exit(f"branch '{args.delete}' not found")
        return
    if args.name:
        sha = refs.current_commit(repo, store)
        if not sha:
            sys.exit("cannot create branch: no commits yet")
        refs.update_ref(repo, f"refs/heads/{args.name}", sha)
        return
    cur_ref = refs.head_ref(repo)
    cur = cur_ref.split("/")[-1] if cur_ref else None
    for name, sha in refs.list_branches(repo):
        mark = "*" if name == cur else " "
        print(f"{mark} {name}")


def cmd_checkout(args):
    repo, store = _repo_and_store(args)
    sha = do_checkout(repo, store, args.target)
    ref = refs.head_ref(repo)
    if ref:
        print(f"Switched to branch '{ref.split('/')[-1]}'")
    else:
        print(f"Detached HEAD at {sha[:7]}")


def _workdir_files(repo):
    out = set()
    for root, _dirs, files in os.walk(repo.workdir):
        if ".git" in os.path.relpath(root, repo.workdir).split(os.sep):
            continue
        for fn in files:
            ap = os.path.join(root, fn)
            rel = os.path.relpath(ap, repo.workdir).replace(os.sep, "/")
            out.add(rel)
    return out


def cmd_status(args):
    repo, store = _repo_and_store(args)
    head = refs.current_commit(repo, store)
    head_files = {}
    if head:
        info = decode_commit(store.read_bytes(head, COMMIT))
        head_files = tree_to_files(store, info["tree"])
    index = read_index(repo)
    staged_new = sorted(set(index) - set(head_files))
    staged_mod = sorted(p for p in set(index) & set(head_files)
                        if index[p] != head_files[p])
    staged_del = sorted(set(head_files) - set(index))
    work = _workdir_files(repo)
    unstaged = []
    for p in sorted(set(index) & work):
        ap = os.path.join(repo.workdir, p)
        with open(ap, "rb") as f:
            data = f.read()
        from .objects import hash_object
        if hash_object(BLOB, data) != index[p]:
            unstaged.append(p)
    untracked = sorted(work - set(index))
    ref = refs.head_ref(repo)
    print(f"On branch {(ref or 'detached HEAD').split('/')[-1]}")
    if staged_new or staged_mod or staged_del:
        print("Staged changes:")
        for p in staged_new:
            print(f"\tnew file:   {p}")
        for p in staged_mod:
            print(f"\tmodified:   {p}")
        for p in staged_del:
            print(f"\tdeleted:    {p}")
    if unstaged:
        print("Unstaged changes:")
        for p in unstaged:
            print(f"\tmodified:   {p}")
    if untracked:
        print("Untracked files:")
        for p in untracked:
            print(f"\t{p}")
    if not (staged_new or staged_mod or staged_del or unstaged or untracked):
        print("nothing to commit, working tree clean")


def _blob_lines(store, blob_sha):
    if blob_sha is None:
        return []
    return store.read_bytes(blob_sha, BLOB).decode(
        "utf-8", errors="replace").splitlines(keepends=True)


def cmd_diff(args):
    repo, store = _repo_and_store(args)
    from .objects import hash_object
    if args.a and args.b:
        sa = refs.resolve(repo, args.a, store)
        sb = refs.resolve(repo, args.b, store)
        if not sa or not sb:
            sys.exit("unknown revision")
        fa = tree_to_files(store, decode_commit(store.read_bytes(sa, COMMIT))["tree"])
        fb = tree_to_files(store, decode_commit(store.read_bytes(sb, COMMIT))["tree"])
        for path in sorted(set(fa) | set(fb)):
            if fa.get(path) == fb.get(path):
                continue
            sys.stdout.write(unified_diff(_blob_lines(store, fa.get(path)),
                                          _blob_lines(store, fb.get(path)),
                                          f"a/{path}", f"b/{path}"))
    else:
        index = read_index(repo)
        for path in sorted(index):
            ap = os.path.join(repo.workdir, path)
            if not os.path.isfile(ap):
                continue
            with open(ap, "rb") as f:
                data = f.read()
            if hash_object(BLOB, data) == index[path]:
                continue
            sys.stdout.write(unified_diff(
                _blob_lines(store, index[path]),
                data.decode("utf-8", errors="replace").splitlines(keepends=True),
                f"a/{path}", f"b/{path}"))


def cmd_gc(args):
    repo, store = _repo_and_store(args)
    from .pack import PackWriter
    objdir = os.path.join(repo.gitdir, "objects")
    writer = PackWriter()
    loose = []
    for sub in os.listdir(objdir):
        subdir = os.path.join(objdir, sub)
        if len(sub) == 2 and os.path.isdir(subdir):
            for name in os.listdir(subdir):
                if len(name) == 38:
                    loose.append(sub + name)
    for sha in loose:
        typ, data = store.read(sha)
        writer.add_object(sha, typ, data)
    if not writer.objects:
        print("nothing to pack")
        return
    packdir = os.path.join(objdir, "pack")
    os.makedirs(packdir, exist_ok=True)
    pname = f"pack-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:12]}.pack"
    pack_path = os.path.join(packdir, pname)
    writer.write(pack_path)
    store.refresh_packs()
    # remove loose objects now packed
    for sha in loose:
        p = os.path.join(objdir, sha[:2], sha[2:])
        if os.path.exists(p):
            os.remove(p)
    print(f"packed {len(writer.objects)} objects into {pname}")


def cmd_merge(args):
    repo, store = _repo_and_store(args)
    other = refs.resolve(repo, args.branch, store)
    if not other:
        sys.exit(f"unknown revision: {args.branch}")
    status, detail = merge_commits(repo, store, other, message=args.message,
                                    branch_name=args.branch)
    if status == "up-to-date":
        print("Already up to date.")
    elif status == "fast-forward":
        print(f"Fast-forwarded to {detail[:7]}.")
    elif status == "merged":
        print(f"Merge made by mini-git: {detail[:7]}.")
    elif status == "conflicts":
        print("CONFLICTS (resolve them, then commit):")
        for p in detail:
            print(f"\t{p}")
        sys.exit(1)


def cmd_merge_file(args):
    from .diff import merge_texts
    repo, _store = _repo_and_store(args)
    texts = []
    for p in (args.current, args.base, args.other):
        ap = os.path.join(repo.workdir, p) if not os.path.isabs(p) else p
        with open(ap, encoding="utf-8") as f:
            texts.append(f.read())
    merged, conflict = merge_texts(texts[1], texts[0], texts[2],
                                    left_label=args.current,
                                    right_label=args.other)
    sys.stdout.write(merged)
    sys.exit(1 if conflict else 0)


def cmd_rebase(args):
    repo, store = _repo_and_store(args)
    status, detail = do_rebase(repo, store, args.upstream)
    if status == "up-to-date":
        print("Current branch is up to date.")
    elif status == "fast-forward":
        print(f"Fast-forwarded to {detail[:7]}.")
    else:
        print(f"Successfully rebased onto {args.upstream} ({detail[:7]}).")


def cmd_verify_pack(args):
    repo, store = _repo_and_store(args)
    total = 0
    deltas = 0
    for reader in store.packs:
        for sha in reader.all_shas():
            typ, _data = reader.read_object(sha)
            total += 1
    print(f"verified {total} packed objects in {len(store.packs)} pack(s)")


def build_parser():
    p = argparse.ArgumentParser(prog="minigit", description="mini-git")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("dir", nargs="?")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("hash-object")
    s.add_argument("-w", action="store_true"); s.add_argument("-t", default="blob")
    s.add_argument("file"); s.set_defaults(fn=cmd_hash_object)
    s = sub.add_parser("cat-file")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("-p", action="store_true"); g.add_argument("-t", action="store_true")
    g.add_argument("-s", action="store_true")
    s.add_argument("object"); s.set_defaults(fn=cmd_cat_file)
    s = sub.add_parser("add"); s.add_argument("paths", nargs="+")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("commit"); s.add_argument("-m", required=True, dest="message")
    s.set_defaults(fn=cmd_commit)
    s = sub.add_parser("log")
    s.add_argument("--oneline", action="store_true")
    s.add_argument("--max-count", type=int, default=0)
    s.add_argument("rev", nargs="?"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("branch")
    s.add_argument("name", nargs="?"); s.add_argument("-d", dest="delete")
    s.set_defaults(fn=cmd_branch)
    s = sub.add_parser("checkout"); s.add_argument("target")
    s.set_defaults(fn=cmd_checkout)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("diff"); s.add_argument("a", nargs="?")
    s.add_argument("b", nargs="?"); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("gc"); s.set_defaults(fn=cmd_gc)
    s = sub.add_parser("merge"); s.add_argument("branch")
    s.add_argument("-m", dest="message"); s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("merge-file")
    s.add_argument("current"); s.add_argument("base"); s.add_argument("other")
    s.set_defaults(fn=cmd_merge_file)
    s = sub.add_parser("rebase"); s.add_argument("upstream")
    s.set_defaults(fn=cmd_rebase)
    s = sub.add_parser("verify-pack"); s.set_defaults(fn=cmd_verify_pack)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
