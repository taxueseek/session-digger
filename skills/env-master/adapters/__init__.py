"""环境适配器包 — 每个 AI 编码环境一个适配器，统一接口。"""

from .claude_adapter import ClaudeAdapter
from .codex_adapter import CodexAdapter
from .grok_adapter import GrokAdapter
from .kimi_adapter import KimiAdapter
from .mimo_adapter import MimoAdapter
from .deepseek_adapter import DeepSeekAdapter
from .cursor_adapter import CursorAdapter
from .aider_adapter import AiderAdapter

ADAPTER_REGISTRY = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "grok": GrokAdapter,
    "kimi": KimiAdapter,
    "mimo": MimoAdapter,
    "deepseek": DeepSeekAdapter,
    "cursor": CursorAdapter,
    "aider": AiderAdapter,
}

ALL_ENVS = list(ADAPTER_REGISTRY.keys())


def get_adapter(env: str):
    """根据环境名返回适配器实例。"""
    cls = ADAPTER_REGISTRY.get(env)
    if cls is None:
        return None
    return cls()
