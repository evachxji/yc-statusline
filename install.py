#!/usr/bin/env python3
"""yc-statusline 一键安装：备份并更新 ~/.claude/settings.json 的 statusLine。"""

import json
import shutil
import sys
import time
from pathlib import Path


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    statusline = script_dir / "statusline.py"
    if not statusline.exists():
        sys.exit("错误：install.py 旁边找不到 statusline.py")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            sys.exit(f"错误：{settings_path} 不是合法 JSON，请先手动修复")

    if sys.platform == "win32":
        # Claude Code 在 Windows 用 git bash 执行 statusline 命令；
        # PYTHONIOENCODING=utf-8 防止 Python 按 GBK 输出进度条字符（█░）崩溃
        command = f'PYTHONIOENCODING=utf-8 python "{statusline.as_posix()}"'
    else:
        python = shutil.which("python3") or shutil.which("python") or "python3"
        command = f'{python} "{statusline.as_posix()}"'

    if settings.get("statusLine") and settings_path.exists():
        backup = settings_path.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        backup.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"检测到已有 statusLine，原配置已备份到 {backup}")

    settings["statusLine"] = {"type": "command", "command": command}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"statusLine 已写入 {settings_path}:\n  {command}")

    if (Path.home() / ".cc-switch" / "cc-switch.db").exists():
        print(
            "\n提示：检测到你使用 CC Switch。它切换供应商时会用自带的公共配置覆盖 "
            "statusLine，请把 CC Switch 数据库 settings 表 common_config_claude 里的 "
            "statusLine 也改成上面这条命令（详见 README 常见问题）。"
        )


if __name__ == "__main__":
    main()
