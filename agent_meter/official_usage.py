"""Official provider usage and shared Claude session token statistics."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CACHE_TTL_SECONDS = 10
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "agent-meter" / "official-usage.json"
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class BalanceInfo:
    currency: str
    total: float
    granted: float = 0.0
    topped_up: float = 0.0


@dataclass
class QuotaWindow:
    name: str
    used: float
    total: float
    reset_at: Optional[float] = None


@dataclass
class ProviderUsage:
    provider: str
    balances: List[BalanceInfo] = field(default_factory=list)
    quotas: List[QuotaWindow] = field(default_factory=list)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    latest_model: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )


@dataclass
class SharedUsage:
    provider: Optional[str]
    provider_usage: Optional[ProviderUsage]
    tokens: TokenUsage
    updated_at: float


def detect_official_provider(base_url: str) -> Optional[str]:
    try:
        parsed = urlparse(base_url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    host = (parsed.hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    kimi_path = parsed.path.rstrip("/")
    if host == "api.kimi.com" and (kimi_path == "/coding" or kimi_path.startswith("/coding/")):
        return "kimi_coding"
    return None


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric quota")
    return float(value)


def _timestamp(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return None


def parse_deepseek_balance(payload: Dict[str, object]) -> ProviderUsage:
    if payload.get("is_available") is False:
        raise ValueError("DeepSeek account balance is unavailable")
    balances = []
    raw_balances = payload.get("balance_infos")
    if not isinstance(raw_balances, list):
        raise ValueError("DeepSeek response is missing balance_infos")
    for item in raw_balances:
        if not isinstance(item, dict):
            continue
        balances.append(
            BalanceInfo(
                currency=str(item.get("currency", "")).upper(),
                total=_number(item["total_balance"]),
                granted=_number(item.get("granted_balance", 0)),
                topped_up=_number(item.get("topped_up_balance", 0)),
            )
        )
    if not balances:
        raise ValueError("DeepSeek response contains no balances")
    return ProviderUsage(provider="deepseek", balances=balances)


def parse_kimi_quota(payload: Dict[str, object]) -> ProviderUsage:
    quotas = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            detail = item.get("detail") if isinstance(item, dict) else None
            if isinstance(detail, dict) and "limit" in detail and "remaining" in detail:
                total = _number(detail["limit"])
                remaining = _number(detail["remaining"])
                quotas.append(
                    QuotaWindow("5h", max(0.0, total - remaining), total, _timestamp(detail.get("resetTime")))
                )
                break
    weekly = payload.get("usage")
    if isinstance(weekly, dict) and "limit" in weekly and "remaining" in weekly:
        total = _number(weekly["limit"])
        remaining = _number(weekly["remaining"])
        quotas.append(
            QuotaWindow("weekly", max(0.0, total - remaining), total, _timestamp(weekly.get("resetTime")))
        )
    if not quotas:
        raise ValueError("Kimi response contains no quota windows")
    return ProviderUsage(provider="kimi_coding", quotas=quotas)


def _request_json(url: str, token: str, expected_host: str) -> Dict[str, object]:
    request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or (final.hostname or "").lower() != expected_host:
            raise ValueError("official usage endpoint redirected to an untrusted host")
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("official usage response is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("official usage response must be an object")
    return payload


def query_official_provider(provider: str, token: str) -> ProviderUsage:
    if provider == "deepseek":
        return parse_deepseek_balance(
            _request_json("https://api.deepseek.com/user/balance", token, "api.deepseek.com")
        )
    if provider == "kimi_coding":
        return parse_kimi_quota(
            _request_json("https://api.kimi.com/coding/v1/usages", token, "api.kimi.com")
        )
    raise ValueError("unsupported official provider")


def _parse_record_time(value: object) -> Optional[float]:
    try:
        return _timestamp(value)
    except (TypeError, ValueError):
        return None


def _read_token_usage(paths: Iterator[Path], earliest: float, latest: float) -> TokenUsage:
    by_message: Dict[str, Dict[str, object]] = {}
    latest_time = 0.0
    latest_model = ""
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict) or record.get("type") != "assistant":
                        continue
                    timestamp = _parse_record_time(record.get("timestamp"))
                    if timestamp is None or timestamp < earliest or timestamp > latest:
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                        continue
                    message_id = message.get("id")
                    if not isinstance(message_id, str) or not message_id:
                        continue
                    previous = by_message.get(message_id)
                    if previous is None or timestamp >= float(previous["timestamp"]):
                        by_message[message_id] = {"timestamp": timestamp, "usage": message["usage"]}
                    model = message.get("model")
                    if timestamp >= latest_time and isinstance(model, str) and model:
                        latest_time, latest_model = timestamp, model
        except (OSError, UnicodeDecodeError):
            continue
    totals = {name: 0 for name in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"
    )}
    for entry in by_message.values():
        usage = entry["usage"]
        if not isinstance(usage, dict):
            continue
        for name in totals:
            value = usage.get(name, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                totals[name] += int(value)
    return TokenUsage(latest_model=latest_model, **totals)


def read_claude_token_usage(projects_root: Path, now: float) -> TokenUsage:
    local_now = datetime.fromtimestamp(now).astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if not projects_root.exists():
        return TokenUsage()
    paths = []
    for path in projects_root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime >= day_start:
                paths.append(path)
        except OSError:
            continue
    return _read_token_usage(iter(paths), day_start, now)


def read_claude_transcript_token_usage(path: Path) -> TokenUsage:
    """统计单个 Claude Code 窗口 transcript 的完整会话 Token。"""
    return _read_token_usage(iter((path,)), 0.0, float("inf"))


def read_latest_transcript_model_info(path: Path) -> tuple[str, float]:
    latest_time = 0.0
    latest_model = ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                timestamp = _parse_record_time(record.get("timestamp")) or 0.0
                model = message.get("model") if isinstance(message, dict) else None
                if timestamp >= latest_time and isinstance(model, str) and model and model != "<synthetic>":
                    latest_time, latest_model = timestamp, model
    except (OSError, UnicodeDecodeError):
        return "", 0.0
    return latest_model, latest_time


def read_latest_transcript_model(path: Path) -> str:
    return read_latest_transcript_model_info(path)[0]


def _provider_from_dict(data: object) -> Optional[ProviderUsage]:
    if not isinstance(data, dict):
        return None
    return ProviderUsage(
        provider=str(data.get("provider", "")),
        balances=[BalanceInfo(**item) for item in data.get("balances", []) if isinstance(item, dict)],
        quotas=[QuotaWindow(**item) for item in data.get("quotas", []) if isinstance(item, dict)],
    )


def _shared_from_dict(data: Dict[str, object]) -> SharedUsage:
    tokens = data.get("tokens")
    return SharedUsage(
        provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
        provider_usage=_provider_from_dict(data.get("provider_usage")),
        tokens=TokenUsage(**tokens) if isinstance(tokens, dict) else TokenUsage(),
        updated_at=float(data.get("updated_at", 0)),
    )


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.with_suffix(path.suffix + ".lock").open("a+")
    except OSError:
        yield
        return
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        handle.close()


def _read_cache(path: Path) -> Optional[SharedUsage]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _shared_from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(path: Path, usage: SharedUsage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(usage), ensure_ascii=False, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def collect_shared_usage(
    base_url: str,
    token: str,
    *,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    cache_path: Path = DEFAULT_CACHE_PATH,
    now: Optional[float] = None,
    query_fn: Callable[[str, str], ProviderUsage] = query_official_provider,
) -> SharedUsage:
    current = time.time() if now is None else now
    provider = detect_official_provider(base_url)
    with _cache_lock(cache_path):
        cached = _read_cache(cache_path)
        if (
            cached is not None
            and cached.provider == provider
            and 0 <= current - cached.updated_at < CACHE_TTL_SECONDS
        ):
            return cached
        provider_usage = None
        if provider and token:
            try:
                provider_usage = query_fn(provider, token)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                provider_usage = None
        shared = SharedUsage(
            provider=provider,
            provider_usage=provider_usage,
            tokens=read_claude_token_usage(projects_root, current),
            updated_at=current,
        )
        try:
            _write_cache(cache_path, shared)
        except OSError:
            pass
        return shared
