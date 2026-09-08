#!/usr/bin/env python3
"""
Claude Code Statusline 显示脚本
在 Claude Code 底部状态栏显示用量信息

数据来源：
- Context: 从 stdin 的 context_window 读取（每个会话独立）
- Usage/Weekly: 从 stdin 的 rate_limits 读取（账号级别，所有会话共享）
- DeepSeek/Kimi: 仅查询已识别的官方 HTTPS 接口
- Session Token: 按 message.id 去重统计当前窗口的完整 transcript

多会话共享机制：
rate_limits 不是每次刷新都会传入（仅 API 响应后才有），
因此收到时写入共享缓存文件，未收到时回退读取缓存，
实现所有会话显示一致的账号用量。
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from agent_meter.official_usage import (
    ProviderUsage,
    collect_shared_usage,
    detect_official_provider,
    read_claude_transcript_token_usage,
    read_latest_transcript_model_info,
)

# ANSI 颜色代码
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    ORANGE = "\033[38;5;208m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


# 共享缓存文件：保存账号级别的 rate_limits，实现多会话同步
CACHE_FILE = Path.home() / ".claude" / "statusline-cache.json"
# 缓存有效期（秒）：超过则视为过期，避免显示陈旧数据
CACHE_TTL = 10

# 输出速率的滚动窗口（秒）：只统计最近这段时间内的 output tokens
RATE_WINDOW_SECONDS = 300
# 每次刷新只读 transcript 尾部，避免大文件全量解析拖慢状态栏
RATE_TAIL_READ_BYTES = 1024 * 1024


def read_stdin_json() -> Optional[Dict[str, Any]]:
    """从 stdin 读取 Claude Code 传入的 JSON 数据"""
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read()
            if data:
                return json.loads(data)
    except Exception:
        pass
    return None


def load_cache() -> Dict[str, Any]:
    """加载共享缓存"""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_cache(rate_limits: Dict[str, Any]) -> None:
    """原子写入共享缓存（避免多会话并发写入损坏文件）"""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rate_limits": rate_limits,
            "updated_at": time.time(),
        }
        # 先写临时文件再 rename，保证原子性
        fd, tmp_path = tempfile.mkstemp(dir=str(CACHE_FILE.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, CACHE_FILE)
    except Exception:
        pass


def parse_reset_at(reset_at: Any) -> Optional[float]:
    """归一化 resets_at：兼容 epoch 数字和 ISO 8601 字符串两种格式"""
    if reset_at is None:
        return None
    if isinstance(reset_at, (int, float)):
        return float(reset_at)
    if isinstance(reset_at, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return None


def is_fresher(incoming: Dict[str, Any], cached: Dict[str, Any]) -> bool:
    """
    判断传入的 rate_limits 是否比缓存的更新。

    依据：5h 窗口内用量单调递增——
    - 传入的 five_hour.resets_at 更大 → 新窗口，更新
    - resets_at 相同且 used_percentage >= 缓存 → 更新
    - 否则视为空闲会话携带的陈旧数据，不允许覆盖
    """
    if not cached:
        return True

    in_5h = incoming.get("five_hour") or {}
    ca_5h = cached.get("five_hour") or {}

    in_reset = parse_reset_at(in_5h.get("resets_at"))
    ca_reset = parse_reset_at(ca_5h.get("resets_at"))

    in_pct = in_5h.get("used_percentage") or 0
    ca_pct = ca_5h.get("used_percentage") or 0

    if ca_reset is None:
        if in_reset is not None:
            return True
        return in_pct > ca_pct
    if in_reset is None:
        return False

    if in_reset > ca_reset:
        return True
    if in_reset < ca_reset:
        return False

    if in_pct != ca_pct:
        return in_pct > ca_pct

    in_7d = incoming.get("seven_day") or {}
    ca_7d = cached.get("seven_day") or {}
    in_7d_reset = parse_reset_at(in_7d.get("resets_at"))
    ca_7d_reset = parse_reset_at(ca_7d.get("resets_at"))
    if ca_7d_reset is None:
        if in_7d_reset is not None:
            return True
    elif in_7d_reset is None:
        return False
    elif in_7d_reset != ca_7d_reset:
        return in_7d_reset > ca_7d_reset

    in_7d_pct = in_7d.get("used_percentage") or 0
    ca_7d_pct = ca_7d.get("used_percentage") or 0
    return in_7d_pct > ca_7d_pct


def has_official_rate_limits(value: Any) -> bool:
    """官方额度必须同时提供 5h/7d 两个数值百分比。"""
    if not isinstance(value, dict):
        return False
    for key in ("five_hour", "seven_day"):
        window = value.get(key)
        if not isinstance(window, dict):
            return False
        percentage = window.get("used_percentage")
        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
            return False
    return True


def has_expired_window(rate_limits: Dict[str, Any], now: Optional[float] = None) -> bool:
    """任一额度窗口已重置时，整份缓存都不再可信。"""
    current_time = time.time() if now is None else now
    for key in ("five_hour", "seven_day"):
        reset_at = parse_reset_at((rate_limits.get(key) or {}).get("resets_at"))
        if reset_at is not None and reset_at <= current_time:
            return True
    return False


def get_rate_limits(stdin_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    获取 rate_limits 数据，实现多会话共享：
    - stdin 数据比缓存新：写入共享缓存并使用
    - stdin 数据陈旧（空闲会话携带的旧值）：不覆盖缓存，改用缓存
    - stdin 无数据：回退到共享缓存（其他会话写入的）
    """
    rate_limits = stdin_data.get("rate_limits")
    if not has_official_rate_limits(rate_limits) or has_expired_window(rate_limits):
        rate_limits = {}

    cache = load_cache()
    cached = cache.get("rate_limits") or {}
    if not has_official_rate_limits(cached) or has_expired_window(cached):
        cached = {}
    updated_at = cache.get("updated_at", 0)
    cache_valid = bool(cached) and (time.time() - updated_at) < CACHE_TTL

    if rate_limits:
        if not cache_valid or is_fresher(rate_limits, cached):
            # 比缓存新（或缓存为空），写入供其他会话使用
            save_cache(rate_limits)
            return rate_limits
        # 陈旧数据：不写缓存；缓存有效则显示缓存，否则退回显示自身
        return cached if cache_valid else rate_limits

    if cache_valid:
        return cached

    return {}


def format_time_remaining(reset_at: Any) -> str:
    """格式化剩余时间（兼容 epoch 数字和 ISO 字符串）"""
    reset_at = parse_reset_at(reset_at)
    if not reset_at:
        return "N/A"

    diff = reset_at - time.time()
    if diff <= 0:
        return "✓"

    hours = int(diff // 3600)
    minutes = int((diff % 3600) // 60)

    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_colored_bar(percentage: float, width: int = 10, color_type: str = "auto") -> str:
    """生成带颜色的进度条"""
    # 百分比限制在 0-100，避免进度条溢出或为负
    percentage = max(0, min(100, percentage))
    filled = int(width * percentage / 100)
    empty = width - filled

    if percentage >= 90:
        color = Colors.RED
    elif percentage >= 70:
        color = Colors.YELLOW
    elif color_type == "cyan":
        color = Colors.CYAN
    else:
        color = Colors.GREEN

    return f"{color}{'█' * filled}{Colors.DIM}{'░' * empty}{Colors.RESET}"


def get_percentage_color(percentage: float) -> str:
    """获取百分比文字颜色"""
    if percentage >= 90:
        return Colors.RED
    elif percentage >= 70:
        return Colors.YELLOW
    return Colors.GREEN


def load_runtime_credentials() -> Tuple[str, str, float]:
    """读取当前 Claude 进程实际使用的地址和令牌，环境变量优先。"""
    configured: Dict[str, Any] = {}
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_modified_at = 0.0
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings_modified_at = settings_path.stat().st_mtime
        if isinstance(settings, dict) and isinstance(settings.get("env"), dict):
            configured = settings["env"]
    except (OSError, ValueError):
        pass
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or configured.get("ANTHROPIC_BASE_URL") or ""
    token = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or configured.get("ANTHROPIC_AUTH_TOKEN")
        or configured.get("ANTHROPIC_API_KEY")
        or ""
    )
    return str(base_url), str(token), settings_modified_at


def get_provider_name(base_url: str) -> str:
    """读取 CC Switch 当前供应商名称；不可用时按官方地址或主机名降级。"""
    database = Path.home() / ".cc-switch" / "cc-switch.db"
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.2)
        try:
            row = connection.execute(
                "SELECT name, settings_config FROM providers "
                "WHERE app_type = 'claude' AND is_current = 1 LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row:
            settings = json.loads(row[1])
            provider_url = ((settings.get("env") or {}).get("ANTHROPIC_BASE_URL") or "")
            if str(provider_url).rstrip("/") == base_url.rstrip("/"):
                return str(row[0])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    official = detect_official_provider(base_url)
    if official == "deepseek":
        return "DeepSeek"
    if official == "kimi_coding":
        return "Kimi"
    if not base_url:
        return "Claude"
    try:
        return urlparse(base_url).hostname or "Claude"
    except ValueError:
        return "Claude"


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def get_quota_color(percentage: float) -> str:
    """Kimi 额度百分比颜色：<70 绿，70-89 橙，>=90 红"""
    if percentage >= 90:
        return Colors.RED
    if percentage >= 70:
        return Colors.ORANGE
    return Colors.GREEN


def format_reset_compact(reset_at: Optional[float]) -> str:
    """额度重置剩余时间，紧凑格式：4h41m / 3d5h / 41m"""
    if not reset_at:
        return ""
    diff = reset_at - time.time()
    if diff <= 0:
        return "✓"
    if diff >= 86400:
        return f"{int(diff // 86400)}d{int((diff % 86400) // 3600)}h"
    hours = int(diff // 3600)
    minutes = int((diff % 3600) // 60)
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def format_tok_rate(rate: float) -> str:
    """输出速率显示格式：87 tok/s / 1.2K tok/s，小于 10 保留一位小数"""
    if rate >= 1000:
        return f"{rate / 1000:.1f}K tok/s"
    if rate >= 10:
        return f"{rate:.0f} tok/s"
    return f"{rate:.1f} tok/s"


def read_recent_output_rate(
    transcript_path: Path, window_seconds: int = RATE_WINDOW_SECONDS
) -> Optional[float]:
    """滚动窗口输出速率（tok/s）。

    参考 claudia-statusline：窗口内 assistant 消息的 output_tokens 去重求和，
    除以有效时长（消息实际跨度，最长取窗口长度，最短 1 秒）；
    窗口内仅一条消息时用 now - 该消息时间作分母，空闲时速率自然衰减到隐藏。
    """
    try:
        size = transcript_path.stat().st_size
        if size == 0:
            return None
        with open(transcript_path, "rb") as f:
            if size > RATE_TAIL_READ_BYTES:
                f.seek(-RATE_TAIL_READ_BYTES, os.SEEK_END)
            data = f.read()
    except OSError:
        return None

    now = time.time()
    cutoff = now - window_seconds
    earliest: Optional[float] = None
    latest: Optional[float] = None
    sum_output = 0
    seen_ids = set()

    for line in data.decode("utf-8", errors="ignore").splitlines():
        if '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        ts = parse_reset_at(entry.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        msg_id = message.get("id") or entry.get("uuid") or ""
        if msg_id:
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        sum_output += (message.get("usage") or {}).get("output_tokens") or 0
        earliest = ts if earliest is None else min(earliest, ts)
        latest = ts if latest is None else max(latest, ts)

    if latest is None:
        return None
    span = (now - earliest) if earliest == latest else (latest - earliest)
    effective = max(1.0, min(span, float(window_seconds)))
    return sum_output / effective


def format_provider_usage(usage: ProviderUsage) -> str:
    if usage.provider == "deepseek":
        symbols = {"CNY": "¥", "USD": "$"}
        values = [
            f"{symbols.get(balance.currency, balance.currency + ' ')}{balance.total:.2f}"
            for balance in usage.balances
        ]
        return f"DS {'/'.join(values)}" if values else ""
    if usage.provider == "kimi_coding":
        values = []
        for window in usage.quotas:
            percentage = (window.used / window.total * 100) if window.total > 0 else 0
            label = "w" if window.name == "weekly" else "h"
            entry = f"{get_quota_color(percentage)}{label}{percentage:.0f}%{Colors.RESET}"
            reset_text = format_reset_compact(window.reset_at)
            if reset_text:
                entry += f" {Colors.DIM}{reset_text}{Colors.RESET}"
            values.append(entry)
        return " ".join(values) if values else ""
    return ""


def apply_model_tag(name: str, model_id: Any) -> str:
    """会话 model id 带 [1m] 等方括号标识时并到显示名上（幂等：显示名已带则不重复）"""
    if not isinstance(model_id, str):
        return name
    match = re.search(r"\[([^\]]+)\]\s*$", model_id)
    if not match:
        return name
    tag = match.group(1).lower()
    base = re.sub(r"\s*\[[^\]]+\]\s*$", "", name).strip() or name
    return f"{base}[{tag}]"


def read_git_branch(start_dir: str) -> Optional[str]:
    """直接读 .git/HEAD 取当前分支（不 spawn git 子进程）；兼容 worktree 的 gitdir 指针"""
    try:
        current = Path(start_dir).resolve()
        for _ in range(10):
            git_entry = current / ".git"
            head_path: Optional[Path] = None
            if git_entry.is_dir():
                head_path = git_entry / "HEAD"
            elif git_entry.is_file():
                for line in git_entry.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("gitdir:"):
                        gitdir = Path(line[7:].strip())
                        if not gitdir.is_absolute():
                            gitdir = (current / gitdir).resolve()
                        head_path = gitdir / "HEAD"
                        break
            if head_path and head_path.is_file():
                head = head_path.read_text(encoding="utf-8", errors="ignore").strip()
                if head.startswith("ref: refs/heads/"):
                    return head[len("ref: refs/heads/"):]
                return head[:7] or None  # detached HEAD：显示短 sha
            if current.parent == current:
                break
            current = current.parent
    except Exception:
        pass
    return None


def main():
    """主函数"""
    stdin_data = read_stdin_json()

    if not stdin_data:
        print("Claude Code")
        return

    base_url, token, provider_changed_at = load_runtime_credentials()
    provider_name = get_provider_name(base_url)

    # 模型信息：供应商切换后的真实响应才可覆盖当前供应商名称。
    model_info = stdin_data.get("model", {})
    model_name = provider_name or model_info.get("display_name") or model_info.get("id") or "Claude"
    is_provider_label = bool(provider_name)
    session_tokens = None
    output_rate = None
    transcript_path = stdin_data.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        candidate = Path(transcript_path).expanduser().resolve()
        projects_root = (Path.home() / ".claude" / "projects").resolve()
        if candidate == projects_root or projects_root in candidate.parents:
            actual_model, response_at = read_latest_transcript_model_info(candidate)
            if actual_model and response_at >= provider_changed_at:
                model_name = actual_model
                is_provider_label = False
            session_tokens = read_claude_transcript_token_usage(candidate)
            output_rate = read_recent_output_rate(candidate)

    # 会话配置的 1m 等标识从 model.id 补上（transcript 里 API 回报的模型名不带该后缀）；
    # 供应商名只是渠道标签，不拼模型标识
    if not is_provider_label:
        model_name = apply_model_tag(model_name, model_info.get("id"))

    # Context 使用率（当前会话独立，stdin 每次都有）
    context_window = stdin_data.get("context_window") or {}
    context_pct = context_window.get("used_percentage") or 0

    # Rate Limits（账号级别，多会话共享）
    rate_limits = get_rate_limits(stdin_data)
    shared_usage = collect_shared_usage(base_url, token)

    bar_ctx = get_colored_bar(context_pct, 10, "cyan")
    pct_ctx_color = get_percentage_color(context_pct)

    # 工作目录 | git 分支（放在 Context 前）
    cwd = (stdin_data.get("workspace") or {}).get("current_dir") or stdin_data.get("cwd") or ""
    dir_name = Path(cwd).name if cwd else ""
    branch = read_git_branch(cwd) if cwd else None

    output = f"{Colors.BOLD}{Colors.CYAN}[{model_name}]{Colors.RESET}"
    if dir_name:
        output += f" │ {dir_name} | {Colors.YELLOW}{branch}{Colors.RESET}" if branch else f" │ {dir_name}"
    output += f" │ Context {bar_ctx} {pct_ctx_color}{context_pct:.0f}%{Colors.RESET}"

    if rate_limits:
        five_hour = rate_limits.get("five_hour") or {}
        util_5h = five_hour.get("used_percentage") or 0
        reset_5h = five_hour.get("resets_at")

        seven_day = rate_limits.get("seven_day") or {}
        util_7d = seven_day.get("used_percentage") or 0
        reset_7d = seven_day.get("resets_at")

        bar_5h = get_colored_bar(util_5h, 10)
        bar_7d = get_colored_bar(util_7d, 10)
        pct_5h_color = get_percentage_color(util_5h)
        pct_7d_color = get_percentage_color(util_7d)
        time_5h = format_time_remaining(reset_5h)
        time_7d = format_time_remaining(reset_7d)

        output += (
            f" │ Usage {bar_5h} {pct_5h_color}{util_5h:.0f}%{Colors.RESET} "
            f"{Colors.DIM}(resets in {time_5h}){Colors.RESET} │ "
            f"Weekly {bar_7d} {pct_7d_color}{util_7d:.0f}%{Colors.RESET} "
            f"{Colors.DIM}(resets in {time_7d}){Colors.RESET}"
        )
    elif shared_usage.provider_usage:
        provider_text = format_provider_usage(shared_usage.provider_usage)
        if provider_text:
            output += f" │ {provider_text}"
    if session_tokens and session_tokens.total_tokens > 0:
        output += f" │ {compact_number(session_tokens.total_tokens)} tok"
    if output_rate is not None:
        output += f" │ {Colors.CYAN}{format_tok_rate(output_rate)}{Colors.RESET}"
    print(output)


if __name__ == "__main__":
    main()
