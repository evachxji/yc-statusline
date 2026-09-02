# yc-statusline 安装指南（供 AI Agent 执行）

> 本文档的读者是 AI 编程助手（Claude Code / Cursor / Codex 等）。请按步骤执行，每步验证后再继续。遇到与文档不符的情况，停下来向用户说明，不要猜测。

## 目标

把 yc-statusline 安装为用户的 Claude Code 底部状态栏。完成后状态栏显示：模型/供应商名、目录与 git 分支、Context 用量、供应商额度（Kimi Coding Plan 用量 / DeepSeek 余额）、会话 token 数、实时输出速率（tok/s）。

## 执行步骤

### 1. 克隆仓库

```bash
git clone https://github.com/evachxji/yc-statusline.git ~/.local/share/yc-statusline
```

已存在则改为 `git -C ~/.local/share/yc-statusline pull`。其他路径也可以，后续命令里的路径跟着换。

### 2. 检查现有配置（必须先备份）

- 读取 `~/.claude/settings.json`。
- 如果已有 `statusLine` 字段（比如 claude-hud 等其他状态栏），**先把现有命令原样展示给用户，确认允许替换后再继续**；无论用户是否确认，修改前都必须备份整个文件（复制为 `settings.json.bak-<时间戳>`）。

### 3. 写入 statusLine

优先运行仓库自带的安装脚本，它会自动处理平台差异：

```bash
python ~/.local/share/yc-statusline/install.py
```

需要手动写时，向 `~/.claude/settings.json` 合并（不要覆盖其他字段）：

- **Windows**：命令必须是 bash 环境变量前缀语法（Claude Code 在 Windows 用 git bash 执行 statusLine）：

  ```json
  "statusLine": {
    "type": "command",
    "command": "PYTHONIOENCODING=utf-8 python \"C:/Users/<用户名>/.local/share/yc-statusline/statusline.py\""
  }
  ```

  ⚠️ `PYTHONIOENCODING=utf-8` 不可省略：Python 在 Windows 管道下默认按 GBK 输出，进度条字符 `█░` 会直接触发 UnicodeEncodeError 导致状态栏空白。

- **macOS / Linux**：

  ```json
  "statusLine": {
    "type": "command",
    "command": "python3 \"$HOME/.local/share/yc-statusline/statusline.py\""
  }
  ```

### 4. CC Switch 用户（存在 ~/.cc-switch/cc-switch.db 时）

CC Switch 切换供应商时会用它数据库里的公共配置覆盖 `settings.json` 的 `statusLine`。需要把同一条 statusLine 命令也写入：

- 数据库 `~/.cc-switch/cc-switch.db` → `settings` 表 → key 为 `common_config_claude` 的行 → 其 JSON 里的 `statusLine` 字段。

跳过此步的后果：用户每次切换供应商，状态栏都会被还原成旧配置。不确定怎么写 SQLite 时，用 Python `sqlite3` 模块读写该行 JSON 即可。

### 5. 自测（必须做）

```bash
echo -n '{"model":{"display_name":"test"},"context_window":{"used_percentage":30}}' | <第3步写入的命令>
```

通过标准：

- 输出一行带 ANSI 颜色码的状态栏文本，无 Python traceback；
- Windows 下若输出报 `UnicodeEncodeError`，说明命令缺了 `PYTHONIOENCODING=utf-8` 前缀，回到第 3 步修正；
- 若输出只有 `Claude Code` 四个字，说明 stdin JSON 没被正确解析（检查测试命令的引号转义），不代表安装失败，但也要向用户说明。

### 6. 向用户汇报

告知：安装完成、statusLine 命令内容、备份文件位置、是否处理了 CC Switch 同步。提醒用户状态栏下次刷新即生效，无需重启。

## 补充说明

- 额度显示无需额外配置 key：脚本复用 Claude Code 的 `ANTHROPIC_BASE_URL` + token。Kimi 需 `https://api.kimi.com/coding`（`sk-kimi-` 开头的 key），DeepSeek 需主机为 `api.deepseek.com`；其他中转地址不会触发余额查询（有意设计）。
- 纯 Python 标准库实现，无需安装任何依赖。
