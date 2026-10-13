"""Diff (Myers O(ND)), unified diff output, and diff3 / 3-way merge."""


def _myers_script(a, b):
    """Myers diff -> forward script of (op, x, y); op in equal/del/ins."""
    n, m = len(a), len(b)
    if n == 0 and m == 0:
        return []
    maxd = n + m
    v = {1: 0}
    trace = []
    done = False
    for d in range(maxd + 1):
        trace.append(dict(v))
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
                x = v.get(k + 1, 0)
            else:
                x = v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[k] = x
            if x >= n and y >= m:
                done = True
                break
        if done:
            break
    script = []
    x, y = n, m
    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y
        if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            script.append(("equal", x - 1, y - 1))
            x -= 1
            y -= 1
        if d == 0:
            break  # round 0's "move" is the imaginary start; nothing to emit
        if x == prev_x:
            script.append(("ins", x, y - 1))
            y -= 1
        else:
            script.append(("del", x - 1, y))
            x -= 1
    script.reverse()
    return script


def opcodes(a, b):
    """Group a Myers script -> [(tag, i1, i2, j1, j2)] like difflib."""
    script = _myers_script(a, b)
    groups = []
    i = j = idx = 0
    n = len(script)
    while idx < n:
        op = script[idx][0]
        i1, j1 = i, j
        while idx < n and script[idx][0] == op:
            if op == "equal":
                i += 1
                j += 1
            elif op == "del":
                i += 1
            else:
                j += 1
            idx += 1
        tag = {"equal": "equal", "del": "delete", "ins": "insert"}[op]
        groups.append((tag, i1, i, j1, j))
    merged = []
    k = 0
    while k < len(groups):
        t, i1, i2, j1, j2 = groups[k]
        if t == "delete" and k + 1 < len(groups) and \
                groups[k + 1][0] == "insert" and groups[k + 1][1] == i2:
            _, _, _, jj1, jj2 = groups[k + 1]
            merged.append(("replace", i1, i2, j1, jj2))
            k += 2
        else:
            merged.append(groups[k])
            k += 1
    return merged


def unified_diff(a_lines, b_lines, path_a="a", path_b="b", context=3):
    ops = opcodes(a_lines, b_lines)
    windows = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        a0 = max(0, i1 - context)
        a3 = min(len(a_lines), i2 + context)
        b0 = j1 - (i1 - a0)
        b3 = j2 + (a3 - i2)
        windows.append([a0, a3, b0, b3])
    merged = []
    for w in windows:
        if merged and w[0] <= merged[-1][1]:
            m = merged[-1]
            m[1] = max(m[1], w[1])
            m[3] = max(m[3], w[3])
        else:
            merged.append(w)
    if not merged:
        return ""
    out = [f"--- {path_a}", f"+++ {path_b}"]
    for a0, a3, b0, b3 in merged:
        out.append(f"@@ -{a0 + 1},{a3 - a0} +{b0 + 1},{b3 - b0} @@")
        for tag, i1, i2, j1, j2 in ops:
            if i2 <= a0 or i1 >= a3:
                continue
            if tag == "equal":
                for x in range(max(i1, a0), min(i2, a3)):
                    out.append(" " + a_lines[x].rstrip("\n"))
            else:
                if tag in ("delete", "replace"):
                    for x in range(i1, i2):
                        out.append("-" + a_lines[x].rstrip("\n"))
                if tag in ("insert", "replace"):
                    for y in range(j1, j2):
                        out.append("+" + b_lines[y].rstrip("\n"))
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- diff3 --

def _change_hunks(base, other):
    """-> [(b_start, b_end, new_lines)]: base spans replaced by new lines."""
    hunks = []
    for tag, i1, i2, j1, j2 in opcodes(base, other):
        if tag != "equal":
            hunks.append((i1, i2, other[j1:j2]))
    return hunks


def diff3_merge(base, left, right):
    """3-way merge of line lists.

    Returns chunks: ("ok", lines) or
    ("conflict", left_lines, base_lines, right_lines).
    """
    ha = _change_hunks(base, left)
    hb = _change_hunks(base, right)
    res = []
    i = 0
    pa = pb = 0
    nb = len(base)
    INF = float("inf")
    while i < nb or pa < len(ha) or pb < len(hb):
        na = ha[pa][0] if pa < len(ha) else INF
        nb_ = hb[pb][0] if pb < len(hb) else INF
        nxt = na if na < nb_ else nb_
        if nxt == INF:
            res.append(("ok", base[i:]))
            break
        if nxt > i:
            res.append(("ok", base[i:nxt]))
            i = nxt
            continue
        end = i
        ca, cb = [], []
        changed = True
        while changed:
            changed = False
            while pa < len(ha) and ha[pa][0] <= end:
                ca.append(ha[pa])
                if ha[pa][1] > end:
                    end = ha[pa][1]
                    changed = True
                pa += 1
            while pb < len(hb) and hb[pb][0] <= end:
                cb.append(hb[pb])
                if hb[pb][1] > end:
                    end = hb[pb][1]
                    changed = True
                pb += 1
        a_lines = [ln for _, _, ls in ca for ln in ls]
        b_lines = [ln for _, _, ls in cb for ln in ls]
        o_lines = base[i:end]
        if not ca:
            a_lines = o_lines
        if not cb:
            b_lines = o_lines
        if a_lines == b_lines:
            res.append(("ok", a_lines))
        elif a_lines == o_lines:
            res.append(("ok", b_lines))
        elif b_lines == o_lines:
            res.append(("ok", a_lines))
        else:
            res.append(("conflict", a_lines, o_lines, b_lines))
        i = end
    return res


def merge_texts(base_text, left_text, right_text,
                left_label="left", right_label="right"):
    """3-way merge of texts -> (merged_text, had_conflict)."""
    base = base_text.splitlines(keepends=True)
    left = left_text.splitlines(keepends=True)
    right = right_text.splitlines(keepends=True)
    out = []
    conflict = False
    for chunk in diff3_merge(base, left, right):
        if chunk[0] == "ok":
            out.extend(chunk[1])
        else:
            conflict = True
            _, a_lines, _o, b_lines = chunk
            out.append(f"<<<<<<< {left_label}\n")
            out.extend(a_lines)
            out.append("=======\n")
            out.extend(b_lines)
            out.append(f">>>>>>> {right_label}\n")
    return "".join(out), conflict
