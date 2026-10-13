"""Repository discovery, HEAD and ref handling."""
import os
import re

_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")


def find_gitdir(start: str) -> str:
    cur = os.path.abspath(start)
    while True:
        gd = os.path.join(cur, ".git")
        if os.path.isdir(gd):
            return gd
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SystemExit("not a mini-git repository (or any parent up to /)")
        cur = parent


class Repo:
    def __init__(self, workdir: str, gitdir: str):
        self.workdir = os.path.abspath(workdir)
        self.gitdir = os.path.abspath(gitdir)

    @classmethod
    def discover(cls, start: str = "."):
        gitdir = find_gitdir(start)
        return cls(os.path.dirname(gitdir), gitdir)

    @classmethod
    def init(cls, path: str, default_branch: str = "main"):
        workdir = os.path.abspath(path)
        gitdir = os.path.join(workdir, ".git")
        os.makedirs(os.path.join(gitdir, "objects", "pack"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "refs", "heads"), exist_ok=True)
        os.makedirs(os.path.join(gitdir, "refs", "tags"), exist_ok=True)
        with open(os.path.join(gitdir, "HEAD"), "w") as f:
            f.write(f"ref: refs/heads/{default_branch}\n")
        with open(os.path.join(gitdir, "config"), "w") as f:
            f.write("[core]\n\trepositoryformatversion = 0\n"
                    "\tfilemode = false\n\tbare = false\n")
        return cls(workdir, gitdir)


def _read_ref_file(repo: Repo, ref: str):
    path = os.path.join(repo.gitdir, ref)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        content = f.read().strip()
    return content or None


def head_contents(repo: Repo) -> str:
    with open(os.path.join(repo.gitdir, "HEAD")) as f:
        return f.read().strip()


def head_ref(repo: Repo):
    """-> 'refs/heads/main' or None when detached."""
    h = head_contents(repo)
    if h.startswith("ref: "):
        return h[5:]
    return None


def resolve(repo: Repo, name: str, store=None):
    """Resolve HEAD / ref / branch short name / sha prefix -> full sha or None."""
    if name == "HEAD":
        h = head_contents(repo)
        if h.startswith("ref: "):
            return _read_ref_file(repo, h[5:])
        return h if _SHA_RE.match(h) else None
    for ref in (name, f"refs/heads/{name}", f"refs/tags/{name}",
                f"refs/remotes/{name}"):
        if ref.startswith("refs/"):
            val = _read_ref_file(repo, ref)
            if val:
                return val
    if _SHA_RE.match(name) and store is not None:
        return store.resolve_prefix(name)
    return None


def update_ref(repo: Repo, ref: str, sha: str):
    if not ref.startswith("refs/"):
        raise ValueError(f"not a ref: {ref}")
    path = os.path.join(repo.gitdir, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(sha + "\n")
    os.replace(tmp, path)


def set_head(repo: Repo, target: str):
    """target: 'refs/heads/x' (attached) or a sha (detached)."""
    with open(os.path.join(repo.gitdir, "HEAD"), "w") as f:
        if target.startswith("refs/"):
            f.write(f"ref: {target}\n")
        else:
            f.write(target + "\n")


def list_branches(repo: Repo):
    d = os.path.join(repo.gitdir, "refs", "heads")
    out = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            val = _read_ref_file(repo, f"refs/heads/{name}")
            if val:
                out.append((name, val))
    return out


def current_commit(repo: Repo, store=None):
    return resolve(repo, "HEAD", store)
