"""PyInstaller 打包入口。

放在包外，避免使用 chatsearch/__main__.py 作为入口时
相对导入（from .app import ...）在冻结环境下失效。
"""
from chatsearch.__main__ import main

if __name__ == "__main__":
    main()
