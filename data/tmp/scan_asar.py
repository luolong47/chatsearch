"""在 app.asar 里定位归档状态相关代码，输出所在文件与上下文。"""
from __future__ import annotations

import json
import re
import struct

ASAR = r"C:\Users\LuoLong\scoop\apps\dsh-desktop\current\resources\app.asar"
NEEDLES = ["archivedSessionIds", "archiveSession", "unarchiveSession"]

with open(ASAR, "rb") as f:
    f.read(4)
    hsize = struct.unpack("<I", f.read(4))[0]
    f.seek(12)
    strlen = struct.unpack("<I", f.read(4))[0]
    root = json.loads(f.read(strlen).decode("utf-8"))
base = 8 + hsize

entries: list[tuple[int, int, str]] = []


def walk(node, path=""):
    for name, meta in (node.get("files") or {}).items():
        cur = path + "/" + name
        if meta.get("files") is not None:
            walk(meta, cur)
        else:
            off, size = int(meta["offset"]), int(meta["size"])
            if size > 0:
                entries.append((off, size, cur))


walk(root)
entries.sort()
data = open(ASAR, "rb").read()
print("files:", len(entries), "| asar MB:", round(len(data) / 1048576, 1))


def locate(rel: int) -> str | None:
    lo, hi = 0, len(entries) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        o, s, p = entries[mid]
        if rel < o:
            hi = mid - 1
        elif rel >= o + s:
            lo = mid + 1
        else:
            return p
    return None


for needle in NEEDLES:
    nb = needle.encode()
    hits = [m.start() - base for m in re.finditer(re.escape(nb), data)]
    print(f"\n=== {needle} -> {len(hits)} 处 ===")
    seen: set[str] = set()
    for rel in hits:
        path = locate(rel)
        if path is None or path in seen:
            continue
        seen.add(path)
        ctx = data[base + rel - 150: base + rel + 200].decode("utf-8", "replace")
        print(" file:", path)
        print("   ", ctx.replace("\n", " "))
        if len(seen) >= 5:
            break
