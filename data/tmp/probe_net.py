"""探针：列出 DSH 渲染进程请求过的 host 端点 + 可见 token 线索。"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

from chatsearch.cdp import _eval, _ws_open, list_pages, read_devtools_port

port = read_devtools_port()
pages = [p for p in list_pages(port) if str(p.get("url", "")).startswith("dsh-app://")]
sock = _ws_open(pages[0]["webSocketDebuggerUrl"], timeout=5)

expr = """
(() => {
  const res = performance.getEntriesByType('resource').map(e => e.name);
  const host = res.filter(u => /127\\.0\\.0\\.1|localhost/.test(u));
  const uniq = [...new Set(host)].slice(0, 60);
  return JSON.stringify({
    total: res.length,
    hostCount: host.length,
    hostUrls: uniq,
    cookie: document.cookie,
    remoteAccess: window.__DSH_REMOTE_ACCESS__ || null,
    hostPaths: window.__DSH_HOST_PATHS__ || null,
  });
})()
"""

raw = _eval(sock, expr)
sock.close()
try:
    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
except Exception:
    print(raw)
