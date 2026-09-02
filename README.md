# yc-statusline

Claude Code 底部状态栏：一行看清当前模型、目录/git 分支、上下文用量、第三方供应商额度（Kimi Coding Plan / DeepSeek 余额）、会话 token 数和实时输出速率。

> A one-line statusline for Claude Code: model, dir/git branch, context usage, third-party provider quota (Kimi Coding Plan / DeepSeek balance), session tokens and live output rate (tok/s). Pure Python stdlib, no dependencies. Windows / macOS / Linux.

基于 [0xYubo/claude-statusline](https://github.com/0xYubo/claude-statusline)（MIT）深度定制，token 速率算法参考 [hagan/claudia-statusline](https://github.com/hagan/claudia-statusline)。

## 效果

```text
[k3[1m]] │ my-project | main │ Context ███░░░░░░░ 38% │ h18% 4h9m w80% 16h9m │ 12.3K tok │ 80 tok/s
```

| 段位 | 含义 |
|---|---|
| `[k3[1m]]` | 当前模型/供应商名（用 CC Switch 时自动显示供应商名） |
| `my-project \| main` | 工作目录 + git 分支（读 `.git/HEAD`，不 spawn 子进程） |
| `Context 38%` | 上下文窗口占用，进度条 + 百分比（≥70% 黄、≥90% 红） |
| `h18% 4h9m` | **5 小时窗口**：用量 18%，4 小时 9 分后重置 |
| `w80% 16h9m` | **周窗口**：用量 80%，16 小时 9 分后重置 |
| `12.3K tok` | 本会话累计 token（按 message.id 去重） |
| `80 tok/s` | 实时输出速率：最近 300 秒滚动窗口的 output tokens/秒 |

h/w 百分比随用量变色：`<70%` 绿、`70–89%` 橙、`≥90%` 红。

## 供应商自动识别

无需配置 key，直接复用 Claude Code 自己的 `ANTHROPIC_BASE_URL` + token（环境变量或 `~/.claude/settings.json` 的 `env` 段）：

| 你的 Claude Code 接入 | 状态栏显示 |
|---|---|
| Kimi Coding Plan（`https://api.kimi.com/coding`） | `h18% 4h9m w80% 16h9m`（5h/周额度 + 重置倒计时） |
| DeepSeek 官方（`https://api.deepseek.com`） | `DeepSeek 余额 ¥86.42` |
| Claude 官方账号 | `Usage` / `Weekly` 用量进度条（5h/7d） |
| 其他中转站 | 不查询（避免把代理额度误报成官方余额），只显示模型名/Context/token |

只向官方固定 HTTPS 域名发请求，数据本地缓存 10 秒，token 不落地。

## 环境要求

- Python 3.8+（只用标准库，无需 pip install 任何依赖）
- Windows 需装有 Git for Windows（Claude Code 在 Windows 上用 git bash 执行状态栏命令）

## 安装

### 方式一：命令行一键安装

macOS / Linux / Windows Git Bash：

```bash
git clone https://github.com/evachxji/yc-statusline.git ~/.local/share/yc-statusline && python ~/.local/share/yc-statusline/install.py
```

Windows PowerShell：

```powershell
git clone https://github.com/evachxji/yc-statusline.git "$env:USERPROFILE\.local\share\yc-statusline"; python "$env:USERPROFILE\.local\share\yc-statusline\install.py"
```

`install.py` 会自动：备份现有 `settings.json`（如已有 statusLine）→ 写入正确的 statusLine 命令（Windows 自动加 `PYTHONIOENCODING=utf-8` 前缀）→ 检测到 CC Switch 时给出提示。

以后更新：

```bash
git -C ~/.local/share/yc-statusline pull
```

### 方式二：Agent 提示词安装

如果你正在用 Claude Code（或其他 AI 编程助手），直接把下面这段贴给它：

```text
帮我安装 yc-statusline 作为 Claude Code 的状态栏（仓库 https://github.com/evachxji/yc-statusline）：
1. git clone 到 ~/.local/share/yc-statusline（已存在则 git pull），然后运行 python ~/.local/share/yc-statusline/install.py
2. 改 ~/.claude/settings.json 之前必须先备份；如果我已有其他 statusLine（比如 claude-hud），先告诉我确认后再替换
3. Windows 下 statusLine 命令必须是 bash 语法的 `PYTHONIOENCODING=utf-8 python "<绝对路径>"`（Claude Code 在 Windows 用 git bash 执行；不加前缀 Python 会按 GBK 输出进度条字符直接崩溃）——install.py 已自动处理，手动改的话注意
4. 如果我使用 CC Switch 切换供应商：还需把 ~/.cc-switch/cc-switch.db 里 settings 表 common_config_claude 中的 statusLine 也改成同一条命令，否则切换供应商时会被覆盖回旧配置
5. 完成后用模拟 stdin JSON 管道测试一次：echo -n '{"model":{"display_name":"test"},"context_window":{"used_percentage":30}}' | <statusLine 命令>，确认输出正常再告诉我
```

## 卸载 / 还原

1. 还原配置：`~/.claude/settings.json` 里删掉 `statusLine` 字段，或恢复安装时自动生成的 `settings.json.bak-<时间戳>` 备份
2. 删除文件：`rm -rf ~/.local/share/yc-statusline`

## 常见问题

**Windows 上状态栏空白 / 报 UnicodeEncodeError？**
Python 在 Windows 管道下默认用 GBK 输出，进度条字符 `█░` 无法编码。statusLine 命令必须以 `PYTHONIOENCODING=utf-8` 开头（bash 环境变量前缀语法）。用 `install.py` 安装会自动处理。

**用 CC Switch 切换供应商后状态栏被改回去了？**
CC Switch 切换时会把它数据库里的"公共配置"合并进 `settings.json`。修复：更新 `~/.cc-switch/cc-switch.db` → `settings` 表 → `common_config_claude` 里的 `statusLine` 为本仓库命令，以后切换就不会再被覆盖。

**Kimi 用量不显示？**
需要 `ANTHROPIC_BASE_URL` 指向 `https://api.kimi.com/coding`（Coding Plan 控制台创建的 `sk-kimi-` 开头的 key）；开放平台 `platform.moonshot.cn` 的 key 查不到套餐用量。

**DeepSeek 余额不显示？**
需要 `ANTHROPIC_BASE_URL` 主机为 `api.deepseek.com`（官方直连）。中转站地址不会触发余额查询，这是有意设计。

**输出速率什么时候显示？**
最近 5 分钟内有 assistant 消息才显示；空闲超过 5 分钟自动隐藏。流式生成中的消息要等该条落盘后才计入，所以看到的是最近已完成的输出速率。

## 致谢

- [0xYubo/claude-statusline](https://github.com/0xYubo/claude-statusline) — 上游基础（供应商识别、用量解析、多会话缓存）
- [hagan/claudia-statusline](https://github.com/hagan/claudia-statusline) — token 滚动窗口速率算法参考

## License

[MIT](LICENSE)
