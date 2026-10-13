"""Packfiles: git pack v2 write/read with deltas, pack index v2.

Writer emits OFS_DELTA objects using git's delta format.
Reader resolves both OFS_DELTA and REF_DELTA.
"""
import hashlib
import os
import struct
import zlib
import binascii

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

_TYPE_TO_NUM = {"commit": OBJ_COMMIT, "tree": OBJ_TREE, "blob": OBJ_BLOB,
                "tag": OBJ_TAG}
_NUM_TO_TYPE = {v: k for k, v in _TYPE_TO_NUM.items()}


# ---------------------------------------------------------------- varints --

def encode_varint(n: int) -> bytes:
    out = bytearray()
    out.append(n & 0x7F)
    n >>= 7
    while n:
        out.append(n & 0x7F)
        n >>= 7
    for i in range(len(out) - 1):
        out[i] |= 0x80  # continuation bit on all but the last byte
    return bytes(out)


def decode_varint(data: bytes, pos: int):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def encode_ofs_offset(offset: int) -> bytes:
    """Git's special offset encoding for OFS_DELTA."""
    buf = [offset & 0x7F]
    offset >>= 7
    while offset:
        offset -= 1
        buf.append(0x80 | (offset & 0x7F))
        offset >>= 7
    buf.reverse()
    return bytes(buf)


def decode_ofs_offset(data: bytes, pos: int):
    c = data[pos]
    pos += 1
    offset = c & 0x7F
    while c & 0x80:
        c = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (c & 0x7F)
    return offset, pos


# ------------------------------------------------------------------ delta --

def create_delta(base: bytes, target: bytes) -> bytes:
    """Build a git-format delta turning base into target."""
    index = {}
    for i in range(len(base) - 3):
        index.setdefault(base[i:i + 4], []).append(i)
    out = bytearray(encode_varint(len(base)))
    out += encode_varint(len(target))
    lit = bytearray()

    def flush_lit():
        while lit:
            chunk = bytes(lit[:127])
            del lit[:len(chunk)]
            out.append(len(chunk))
            out.extend(chunk)  # NB: '+=' would rebind the closure name

    i = 0
    n = len(target)
    while i < n:
        best_len, best_off = 0, 0
        if i <= n - 4:
            for off in index.get(target[i:i + 4], ()):
                maxl = min(n - i, len(base) - off, 0x10000)
                l = 4
                while l < maxl and target[i + l] == base[off + l]:
                    l += 1
                if l > best_len:
                    best_len, best_off = l, off
        if best_len >= 8:
            flush_lit()
            flag = 0x80
            extra = bytearray()
            off, size = best_off, best_len
            for bit in range(4):
                b = (off >> (8 * bit)) & 0xFF
                if b:
                    flag |= 1 << bit
                    extra.append(b)
            for bit in range(3):
                b = (size >> (8 * bit)) & 0xFF
                if b:
                    flag |= 1 << (4 + bit)
                    extra.append(b)
            out.append(flag)
            out += extra
            i += best_len
        else:
            lit.append(target[i])
            i += 1
    flush_lit()
    return bytes(out)


def apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0
    base_size, pos = decode_varint(delta, pos)
    result_size, pos = decode_varint(delta, pos)
    if base_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (1 << (4 + bit)):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        else:
            size = op & 0x7F
            if size == 0:
                raise ValueError("invalid delta opcode 0")
            out += delta[pos:pos + size]
            pos += size
    if len(out) != result_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


# ------------------------------------------------------------- pack write --

def _pack_obj_header(type_num: int, size: int) -> bytes:
    out = bytearray()
    c = (type_num << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


class PackWriter:
    """Collect objects, then write <name>.pack + <name>.idx (v2)."""

    def __init__(self):
        self.objects = []  # (sha, type, data)
        self._seen = set()

    def add_object(self, sha: str, obj_type: str, data: bytes):
        if sha not in self._seen:
            self._seen.add(sha)
            self.objects.append((sha, obj_type, data))

    def write(self, pack_path: str):
        order = {"commit": 0, "tree": 1, "tag": 2, "blob": 3}
        objs = sorted(self.objects, key=lambda o: (order[o[1]], o[0]))
        base_candidates = []  # (sha, type, data, offset) of emitted objects
        records = []  # (sha, offset, raw_record_bytes)
        buf = bytearray()
        buf += b"PACK" + struct.pack(">I", 2) + struct.pack(">I", len(objs))

        for sha, typ, data in objs:
            best = None  # (delta_len, base_offset, delta)
            if typ == "blob":
                for _csha, ctyp, cdata, coff in base_candidates[-32:]:
                    if ctyp != typ:
                        continue
                    if abs(len(cdata) - len(data)) > len(data):
                        continue
                    delta = create_delta(cdata, data)
                    if len(delta) < len(data) and \
                            (best is None or len(delta) < best[0]):
                        best = (len(delta), coff, delta)
            if best is not None:
                _dlen, base_off, delta = best
                obj_off = len(buf)
                rec = (_pack_obj_header(OBJ_OFS_DELTA, len(delta))
                       + encode_ofs_offset(obj_off - base_off)
                       + zlib.compress(delta))
                buf += rec
                records.append((sha, obj_off, bytes(rec)))
            else:
                obj_off = len(buf)
                rec = (_pack_obj_header(_TYPE_TO_NUM[typ], len(data))
                       + zlib.compress(data))
                buf += rec
                records.append((sha, obj_off, bytes(rec)))
                base_candidates.append((sha, typ, data, obj_off))

        pack_sha = hashlib.sha1(bytes(buf)).digest()
        buf += pack_sha
        with open(pack_path, "wb") as f:
            f.write(bytes(buf))
        self._write_idx(pack_path[:-5] + ".idx", records, pack_sha)
        return pack_path

    def _write_idx(self, idx_path, records, pack_sha: bytes):
        recs = sorted(records, key=lambda r: r[0])  # sort by sha
        n = len(recs)
        fanout = [0] * 256
        for sha, _off, _rec in recs:
            fanout[int(sha[:2], 16)] += 1
        for i in range(1, 256):
            fanout[i] += fanout[i - 1]
        out = bytearray()
        out += b"\xfftOc" + struct.pack(">I", 2)
        for c in fanout:
            out += struct.pack(">I", c)
        for sha, _off, _rec in recs:
            out += bytes.fromhex(sha)
        for _sha, _off, rec in recs:
            out += struct.pack(">I", binascii.crc32(rec) & 0xFFFFFFFF)
        large = []
        for _sha, off, _rec in recs:
            if off >= 0x80000000:
                large.append(off)
                out += struct.pack(">I", 0x80000000 | (len(large) - 1))
            else:
                out += struct.pack(">I", off)
        for off in large:
            out += struct.pack(">Q", off)
        out += pack_sha
        idx_sha = hashlib.sha1(bytes(out)).digest()
        out += idx_sha
        with open(idx_path, "wb") as f:
            f.write(bytes(out))


# -------------------------------------------------------------- pack read --

class PackReader:
    """Read a pack v2 via its idx v2. Set .store for REF_DELTA fallback."""

    def __init__(self, pack_path: str, idx_path: str = None):
        self.pack_path = pack_path
        self.idx_path = idx_path or pack_path[:-5] + ".idx"
        self.store = None
        self._offsets = {}
        self._shas = []
        self._parse_idx()
        self._f = open(pack_path, "rb")

    def _parse_idx(self):
        with open(self.idx_path, "rb") as f:
            data = f.read()
        if data[:4] != b"\xfftOc":
            raise ValueError("not a v2 pack index")
        version = struct.unpack(">I", data[4:8])[0]
        if version != 2:
            raise ValueError(f"unsupported idx version {version}")
        fanout = struct.unpack(">256I", data[8:1032])
        n = fanout[255]
        pos = 1032
        shas = [data[pos + i * 20:pos + (i + 1) * 20].hex()
                for i in range(n)]
        pos += n * 20
        pos += n * 4  # crc32s
        offsets = struct.unpack(f">{n}I", data[pos:pos + n * 4])
        pos += n * 4
        large_idx = []
        real_offsets = []
        for off in offsets:
            if off & 0x80000000:
                large_idx.append(off & 0x7FFFFFFF)
                real_offsets.append(None)
            else:
                real_offsets.append(off)
        if large_idx:
            n64 = max(large_idx) + 1
            big = struct.unpack(f">{n64}Q", data[pos:pos + n64 * 8])
            real_offsets = [big[i] if o is None else o
                            for o, i in zip(real_offsets, large_idx)]
        self._shas = shas
        self._offsets = dict(zip(shas, real_offsets))

    def contains(self, sha: str) -> bool:
        return sha in self._offsets

    def all_shas(self):
        return list(self._shas)

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass

    def _inflate_from(self, f) -> bytes:
        decomp = zlib.decompressobj()
        out = bytearray()
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            out += decomp.decompress(chunk)
            if decomp.eof:
                break
        return bytes(out)

    def _read_at(self, offset: int):
        f = self._f
        f.seek(offset)
        b = f.read(1)[0]
        type_num = (b >> 4) & 0x07
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = f.read(1)[0]
            size |= (b & 0x7F) << shift
            shift += 7
        data_start = f.tell()
        if type_num == OBJ_OFS_DELTA:
            raw = f.read(16)
            delta_off, pos = decode_ofs_offset(raw, 0)
            f.seek(data_start + pos)
            delta = self._inflate_from(f)  # read before recursing: the
            base_off = offset - delta_off  # recursive call moves the cursor
            base_type, base_data = self._read_at(base_off)
            return base_type, apply_delta(base_data, delta)
        if type_num == OBJ_REF_DELTA:
            base_sha = f.read(20).hex()
            delta = self._inflate_from(f)
            if base_sha in self._offsets:
                base_type, base_data = self._read_at(self._offsets[base_sha])
            elif self.store is not None:
                base_type, base_data = self.store.read(base_sha)
            else:
                raise KeyError(f"delta base not in pack: {base_sha}")
            return base_type, apply_delta(base_data, delta)
        if type_num in _NUM_TO_TYPE:
            data = self._inflate_from(f)
            if len(data) != size:
                raise ValueError("pack object size mismatch")
            return _NUM_TO_TYPE[type_num], data
        raise ValueError(f"unknown pack object type {type_num}")

    def read_object(self, sha: str):
        if sha not in self._offsets:
            raise KeyError(sha)
        return self._read_at(self._offsets[sha])
