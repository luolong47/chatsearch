# chatsearch

> 本地聊天记录全文检索。把 **WorkBuddy** 和 **DeepSeek Harness** 的会话统一到一处搜索，跑在你自己的机器上，不联网、不上传。

Windows 托盘常驻 + 本地网页界面。标题、正文、代码块都能命中，结果里直接高亮。

---

## 30 秒开跑

需要 [Python 3.11+](https://www.python.org/) 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
git clone https://github.com/luolong47/chatsearch.git
cd chatsearch
uv sync
uv run python -m chatsearch.tray
```

托盘区出现**蓝色放大镜**就成功了。左键点它，或直接访问 <http://127.0.0.1:8765>。

Windows 上更懒的办法：装好 uv 后直接双击仓库里的 **`启动聊天检索.bat`**，它自己会 `uv sync` 再拉托盘。

只想开网页、不要托盘：

```bash
uv run python -m chatsearch.app
```

### 用 Scoop 装（免 Python 环境）

```powershell
scoop bucket add myapps https://github.com/luolong47/myapps
scoop install myapps/chatsearch
```

装的是打包好的单文件 exe，不需要 Python / uv。索引数据放在 scoop 的 `persist` 目录，升级不会丢。

---

## 它读哪些文件

| 内容 | 路径 |
|---|---|
| WorkBuddy 会话 | `~/.workbuddy/projects/<slug>/<session>.jsonl` |
| WorkBuddy 标题 / 归档状态 | `~/.workbuddy/workbuddy.db` |
| DeepSeek Harness 会话 | `~/.dsh/sessions/<workspace>/session-<id>/session.v4.jsonl.zstd` |
| DSH 归档状态 | `~/.dsh/storages/workspace.json` |

**全部只读**，不会改动原始记录。索引落在 `data/index.sqlite3`，删掉会自动重建。

---

## 搜法

- 空格分词，中英文混排都能切：`托盘 图标`、`markdown 渲染`
- **标题也参与检索**，不只在正文里翻
- 侧栏可按 **来源**（WorkBuddy / DSH）、**工作目录**、**是否归档** 过滤
- 结果按「轮」聚合：同一轮里 assistant 的多条碎片合成一条，不会刷屏
- 详情页右侧有刻度导航条，点一下直接跳到对应消息
- 深色 / 浅色主题，右上角一键切
- 归档的会话可以直接在详情页点 **取消归档** 写回原生应用（写前自动备份到 `data/backups/`，DSH 侧还会重载渲染进程让其立刻刷新）

---

## 托盘菜单

| 项 | 作用 |
|---|---|
| 打开检索页面 | 打开浏览器 |
| 更新索引（增量） | 只处理变动文件，通常不到 1 秒 |
| 重建索引（全量） | 全部重扫 |
| 重启服务 | 端口卡住时用 |
| 打开数据目录 | 定位到 `data/` |
| 发送到桌面快捷方式 | 在桌面生成 `.lnk` |
| 开机自动启动 | 勾选即生效，写启动目录 VBS，**不建计划任务、不弹控制台** |
| 退出 | 停服务并移除图标 |

文件改动由 watchdog 实时监听，正常情况下不需要手动更新。

---

## 命令行

```bash
uv run python -m chatsearch.app --port 9000      # 换端口
uv run python -m chatsearch.app --no-browser     # 不自动开浏览器
uv run python -m chatsearch.app --rebuild        # 启动前全量重建
uv run python -m chatsearch.tray --no-browser    # 托盘：静默启动
```

---

## 常见问题

**托盘图标找不到？**
Win11 默认把新图标塞进 `^` 隐藏区。程序启动时会自动写注册表 `IsPromoted=1` 把它提升到常显。

**端口 8765 被占？**
托盘启动时会自动查 `GetExtendedTcpTable` 并结束占用进程，不需要手动 taskkill。

**索引是空的？**
说明本机没有 WorkBuddy / DSH 的数据，或路径不同。改 `src/chatsearch/config.py` 顶部的路径常量即可。

**会不会把聊天记录传到网上？**
不会。服务只绑定 `127.0.0.1`，唯一的网络动作是打开本地页面。

**支持 macOS / Linux？**
核心检索和网页界面是跨平台的；托盘和开机自启是 Windows 专用实现（纯 Win32），其他平台用 `chatsearch.app` 起网页即可。

---

## 自己打包

```powershell
uv sync
pwsh -File build.ps1
```

产物为 `dist\chatsearch.exe`：单文件、无控制台（`--windowed`），可直接拷到任意目录运行，`data\` 会自动建在 exe 同级。

打包版与源码版的入口一致：不带参数起托盘，`chatsearch.exe app --port 9000` 只开网页。

---

## 技术栈

Python 3.11 · sqlite3 FTS5(trigram) · http.server · watchdog · zstandard · 纯手写 Win32 托盘（ctypes，无 pystray）

前端：原生 JS + marked + highlight.js + github-markdown-css，全部本地化，无 CDN 依赖，断网可用。

---

## License

MIT
