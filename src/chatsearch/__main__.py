"""统一入口：默认起托盘；`app` 子命令只起网页服务。

源码运行：`python -m chatsearch`
PyInstaller 打包：exe 双击即托盘，`chatsearch.exe app --port 9000` 只开网页
"""
from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("app", "web", "--app"):
        sys.argv = [sys.argv[0], *argv[1:]]
        from .app import main as app_main

        app_main()
        return
    from .tray import main as tray_main

    tray_main()


if __name__ == "__main__":
    main()
