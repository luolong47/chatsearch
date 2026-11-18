# chatsearch

本地聊天记录全文检索。把 **WorkBuddy** 和 **DeepSeek Harness** 的会话统一到一处搜——标题、正文、代码块都能命中，只读本地数据，不联网。

Windows 托盘常驻 + 本地网页（<http://127.0.0.1:8765>）。

## 装

**Scoop（免 Python，推荐）**

```powershell
scoop bucket add myapps https://github.com/luolong47/myapps
scoop install myapps/chatsearch
```

**源码**（需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)）

```bash
git clone https://github.com/luolong47/chatsearch.git && cd chatsearch
uv sync && uv run python -m chatsearch.tray
```

Windows 更懒：装好 uv 后双击仓库里的 `启动聊天检索.bat`。只开网页不要托盘用 `uv run python -m chatsearch.app`。

## 用

- **搜**：空格分词，中英混排都能切；侧栏按来源 / 工作目录 / 是否归档过滤
- **看**：结果按「轮」聚合；详情页右侧刻度导航条直接跳消息；深色浅色一键切
- **归档 / 取消归档**：详情页或批量操作，写回原生 App（写前自动备份）
- **同步**：转成另一端原生格式并登记进去，之后能在另一个 App 里接着聊。只搬文本对话，不含工具调用、推理、附件
- **批量**：筛选栏「☑ 批量」→ 勾选 → 底部归档 / 取消归档 / 同步，可混合来源，一次写到底
- **进行中的会话不能归档、不能同步**：会话文件 5 分钟内被写过，或 WorkBuddy 原生状态为 working
- **同步是幂等的**：内容相同不重复写；源端归档状态跟随副本；登记在索引库 `migrations` 表，重建索引不丢

## 数据

| 内容 | 路径 |
|---|---|
| WorkBuddy 会话 / 标题归档 | `~/.workbuddy/projects/<slug>/*.jsonl` · `~/.workbuddy/workbuddy.db` |
| DSH 会话 / 归档 | `~/.dsh/sessions/*/session-*/session.v4.jsonl.zstd` · `~/.dsh/storages/workspace.json` |

全部只读。索引在 `data/index.sqlite3`，删掉自动重建；写入前备份落在 `data/backups/`（每类留最近 20 份）。路径不对就改 `src/chatsearch/config.py` 顶部常量。

两个坑值得知道：

- **同一段对话在 WorkBuddy 里可能存了多份**（按登录态各一份 `user_id` 行 + jsonl，侧边栏只列当前账号那份）。归档 / 取消归档会整组一起改，否则改了 App 里没反应。
- **原生 App 只在启动时读一次列表**。外部改完状态后，WorkBuddy 里要重载界面（`Ctrl+R` 或重开窗口）才看得到；DSH 需要重启应用。

## 托盘 / 命令行

托盘菜单：打开页面 · 更新索引 · 重建索引 · 重启服务 · 打开数据目录 · 桌面快捷方式 · 开机自启 · 退出。文件改动由 watchdog 实时监听，正常无需手动更新。

```bash
uv run python -m chatsearch.app --port 9000    # 换端口
uv run python -m chatsearch.app --rebuild      # 启动前全量重建
uv run python -m chatsearch.tray --no-browser  # 静默启动
```

## 自己打包

```powershell
uv sync; pwsh -File build.ps1
```

产物 `dist\chatsearch.exe`：单文件、无控制台，`data\` 建在 exe 同级。打包版入口与源码版一致。

## 杂项

- 托盘图标藏在 `^` 区？启动时自动写注册表提升到常显
- 端口被占？启动时自动结束占用进程
- 索引空？本机没有对应数据，或路径不同
- 会上传吗？不会，只绑 `127.0.0.1`
- macOS / Linux？检索和网页跨平台，托盘与自启是纯 Win32

技术栈：Python 3.11 · sqlite3 FTS5(trigram) · http.server · watchdog · zstandard · 纯手写 Win32 托盘；前端原生 JS + marked + highlight.js，全部本地化，断网可用。

MIT
