"""Git-compatible loose object store: blob/tree/commit, zlib, SHA-1.

Loose object layout is byte-identical to git:
    zlib("<type> <size>\\0<content>")
stored at .git/objects/aa/bb... with SHA-1 = sha1(header + content).
"""
import hashlib
import os
import zlib

BLOB = "blob"
TREE = "tree"
COMMIT = "commit"
TAG = "tag"

_VALID_TYPES = {BLOB, TREE, COMMIT, TAG}


def hash_object(obj_type: str, data: bytes) -> str:
    """SHA-1 id of an object, exactly as git computes it."""
    if obj_type not in _VALID_TYPES:
        raise ValueError(f"bad object type: {obj_type}")
    header = f"{obj_type} {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _loose_path(gitdir: str, sha: str) -> str:
    return os.path.join(gitdir, "objects", sha[:2], sha[2:])


def encode_tree(entries) -> bytes:
    """entries: iterable of (mode:int, name:str, sha:str). Sorted by name like git."""
    out = bytearray()
    for mode, name, sha in sorted(entries, key=lambda e: e[1]):
        if "/" in name or "\0" in name:
            raise ValueError(f"bad tree entry name: {name!r}")
        out += f"{mode:o} ".encode("ascii") + name.encode("utf-8") + b"\0"
        out += bytes.fromhex(sha)
    return bytes(out)


def decode_tree(data: bytes):
    """Parse tree content -> list of (mode:int, name:str, sha:str)."""
    entries = []
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        mode = int(data[pos:sp].decode("ascii"), 8)
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8")
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        pos = nul + 21
    return entries


def encode_commit(tree: str, parents, author: str, committer: str,
                   message: str) -> bytes:
    """author/committer: 'Name <email> <unix-ts> <tz>' e.g. 'A <a@x> 123 +0700'."""
    lines = [f"tree {tree}"]
    for p in parents:
        lines.append(f"parent {p}")
    lines.append(f"author {author}")
    lines.append(f"committer {committer}")
    text = "\n".join(lines) + "\n\n" + message
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def decode_commit(data: bytes) -> dict:
    text = data.decode("utf-8")
    head, _, message = text.partition("\n\n")
    info = {"parents": []}
    for line in head.split("\n"):
        if line.startswith("tree "):
            info["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            info["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            info["author"] = line[7:]
        elif line.startswith("committer "):
            info["committer"] = line[10:]
    info["message"] = message
    return info


class ObjectStore:
    """Read/write objects: loose files first, then packfiles."""

    def __init__(self, gitdir: str):
        self.gitdir = gitdir
        self._packs = None  # lazy list of PackReader

    # -- packs (lazy) -----------------------------------------------------
    def _load_packs(self):
        from .pack import PackReader  # deferred: pack imports nothing from here
        packs = []
        packdir = os.path.join(self.gitdir, "objects", "pack")
        if os.path.isdir(packdir):
            for name in sorted(os.listdir(packdir)):
                if name.endswith(".idx"):
                    pack_name = name[:-4] + ".pack"
                    pack_path = os.path.join(packdir, pack_name)
                    if os.path.exists(pack_path):
                        packs.append(PackReader(pack_path,
                                              os.path.join(packdir, name)))
        self._packs = packs
        return packs

    @property
    def packs(self):
        if self._packs is None:
            return self._load_packs()
        return self._packs

    def refresh_packs(self):
        self._packs = None

    def _find_pack(self, sha: str):
        for reader in self.packs:
            if reader.contains(sha):
                return reader
        return None

    # -- write ------------------------------------------------------------
    def write(self, obj_type: str, data: bytes) -> str:
        sha = hash_object(obj_type, data)
        if self.exists(sha):
            return sha
        path = _loose_path(self.gitdir, sha)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = f"{obj_type} {len(data)}\0".encode("ascii") + data
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(zlib.compress(raw))
        os.replace(tmp, path)
        return sha

    def exists(self, sha: str) -> bool:
        if not (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)):
            return False
        if os.path.exists(_loose_path(self.gitdir, sha)):
            return True
        return self._find_pack(sha) is not None

    # -- read -------------------------------------------------------------
    def read(self, sha: str):
        """-> (type:str, data:bytes). Raises KeyError if missing."""
        sha = sha.lower()
        path = _loose_path(self.gitdir, sha)
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = zlib.decompress(f.read())
            nul = raw.index(b"\0")
            typ, size = raw[:nul].decode("ascii").split(" ", 1)
            data = raw[nul + 1:]
            if len(data) != int(size):
                raise ValueError(f"size mismatch in loose object {sha}")
            return typ, data
        reader = self._find_pack(sha)
        if reader is None:
            raise KeyError(f"object not found: {sha}")
        return reader.read_object(sha)

    def read_bytes(self, sha: str, expected_type: str = None) -> bytes:
        typ, data = self.read(sha)
        if expected_type and typ != expected_type:
            raise ValueError(f"object {sha} is {typ}, expected {expected_type}")
        return data

    def resolve_prefix(self, prefix: str):
        """Resolve a (possibly abbreviated) sha -> full sha or None."""
        prefix = prefix.lower()
        if len(prefix) == 40 and self.exists(prefix):
            return prefix
        if len(prefix) < 4:
            return None
        found = []
        objdir = os.path.join(self.gitdir, "objects")
        sub = os.path.join(objdir, prefix[:2])
        if os.path.isdir(sub):
            for name in os.listdir(sub):
                if name.startswith(prefix[2:]) and len(name) == 38:
                    found.append(prefix[:2] + name)
        for reader in self.packs:
            for sha in reader.all_shas():
                if sha.startswith(prefix):
                    found.append(sha)
        uniq = sorted(set(found))
        return uniq[0] if len(uniq) == 1 else None
