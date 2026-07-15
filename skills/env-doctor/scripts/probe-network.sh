#!/usr/bin/env bash
#
# probe-network.sh — 探测所有 AI 编码环境的 API 可达性
#
# 为什么原生做不到：每个环境的 base_url 不同，
# 原生命令只看自己的链路。此脚本集中探测所有环境的 API。
#
# URL 发现策略（自适应，不依赖固定路径）：
#   1. 扫描所有已知配置文件中符合 URL 模式的值
#   2. 从环境变量中提取 URL 配置
#   3. 去重、分类、探测
#
# 输出: JSON（无表情符号）
# 用法: bash scripts/probe-network.sh [--timeout 5] [--json]

exec python3 - "$@" <<'PYEOF'
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import ssl
from pathlib import Path
from collections import OrderedDict

HOME = Path.home()
TIMEOUT = 5

# ── URL 发现引擎（自适应） ────────────────────────────────

# 匹配 URL 的正则（严格的完整 URL：host 必须是有效域名或 IP）
URL_PATTERN = re.compile(
    r'(?:https?://)'
    r'(?:'
    # 域名
    r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+'
    r'[a-zA-Z]{2,}'
    r'|'
    # 或 IPv4（完整四段）
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r')'
    r'(?::\d{2,5})?'
    r'(?:/[^\s"\']*)?'
)

# 配置文件中指示「这是 API URL」的关键名
URL_KEY_HINTS = [
    'base_url', 'api_url', 'endpoint', 'api_base', 'base_uri',
    'ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL', 'API_URL',
    'server_url', 'gateway_url', 'proxy_url',
]

# 不需要扫描的「噪声文件」——依赖清单、lock 文件等
NOISE_FILES = {
    'package-lock.json', 'package.json', '.package-lock.json',
    'yarn.lock', 'pnpm-lock.yaml', 'bun.lock', 'Cargo.lock',
    'go.sum', 'Gemfile.lock', 'composer.lock',
}

# 不需要探测的 host 段——CDN、包管理、文档站点
NOISE_HOSTS = [
    'cdn.', 'unpkg.', 'jsdelivr.', 'googleapis.', 'npmjs.',
    'registry.npmmirror().' , 'githubusercontent.', 'avatars.',
    'opencollective.', 'sponsors.', 'fast-check.dev',
    'zod.dev', 'effect.website', 'eemeli.org', 'standardschema.dev',
    'json.schemastore.org', 'mootools.net', 'delved.org',
]


def is_noise_url(url: str) -> bool:
    """判断 URL 是否为噪声（非 API 端点）"""
    u = url.lower()
    host = re.match(r'https?://([^/:]+)', u)
    if host:
        host_str = host.group(1)
        for noise in NOISE_HOSTS:
            if noise in host_str:
                return True
    # github 非 API 链接
    if 'github.com/' in u and not any(x in u for x in ['api.github.com', '/repos/', '/raw.']):
        return True
    return False


def extract_urls_from_file(path: Path) -> dict[str, str]:
    """从任意配置文件中提取所有 URL，附带关键名上下文"""
    found = {}
    if not path.exists():
        return found
    # 跳过噪声文件
    if path.name in NOISE_FILES:
        return found
    try:
        content = path.read_text(errors='replace')
    except Exception:
        return found

    # 方法 1: key = "value" / key = 'value' 格式（TOML / JSON / env）
    # 这是最可信的来源（显式配置的 API URL）
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('#') or line.startswith('//'):
            continue
        for hint in URL_KEY_HINTS:
            m = re.search(
                r'(?:["\']?' + re.escape(hint) + r'["\']?)\s*[=:]\s*["\']([^"\']+)["\']',
                line, re.IGNORECASE
            )
            if m:
                url = m.group(1)
                if URL_PATTERN.match(url):
                    key = f"{path.name}:{hint}"
                    found[key] = url

    # 方法 2: 只在明确是 AI 编码工具的配置文件中做通用 URL 提取
    known_config_names = {
        'settings.json', 'config.toml', 'channels.json',
        'openclaw.json', 'opencode.json', 'mimocode.json',
    }
    if path.name in known_config_names:
        for m in URL_PATTERN.finditer(content):
            url = m.group(0)
            if is_noise_url(url):
                continue
            host = re.match(r'https?://([^/:]+)', url)
            if host:
                key = f"{path.name}:auto:{host.group(1)}"
                if key not in found and url not in found.values():
                    found[key] = url

    return found


def extract_urls_from_env() -> dict[str, str]:
    """从进程环境变量中提取 URL 配置"""
    found = {}
    for key, val in os.environ.items():
        if any(h.lower() in key.lower() for h in URL_KEY_HINTS):
            if URL_PATTERN.match(val):
                found[f"env:{key}"] = val
    return found


def discover_all_urls() -> dict[str, str]:
    """全面发现：所有配置文件 + 环境变量"""
    all_urls = OrderedDict()

    # 已知的配置文件位置（固定扫描位）
    config_files = [
        # Claude Code
        HOME / ".claude/settings.json",
        # Codex
        HOME / ".codex/config.toml",
        HOME / "/Library/Application Support/orca/codex-runtime-home/home/config.toml",
        # Grok
        HOME / ".grok/config.toml",
        # Kimi Code
        HOME / ".kimi-code/config.toml",
        # MiMo Code
        HOME / ".mimocode/config.toml",
    ]

    # 额外的自动发现：扫描 ~/.config/ 和 ~/ 下所有 .toml / .json 中的 URL
    config_home = HOME / ".config"
    if config_home.exists():
        for toml in config_home.rglob("*.toml"):
            if toml.name not in NOISE_FILES:
                config_files.append(toml)
        for jsonf in config_home.rglob("*.json"):
            if jsonf.name not in NOISE_FILES and jsonf.stat().st_size < 100_000:
                config_files.append(jsonf)

    # 顶级隐藏目录的 config 文件（排除 lock 文件）
    for d in HOME.iterdir():
        if d.is_dir() and d.name.startswith('.') and d.name not in ('.', '..'):
            for ext in ('*.toml', '*.json'):
                for f in d.glob(ext):
                    if f.name not in NOISE_FILES and f.stat().st_size < 100_000:
                        # 只扫描明确是配置/通道/模型的文件名
                        if any(k in f.name.lower() for k in
                               ['config', 'channel', 'model', 'setting',
                                'openclaw', 'opencode', 'mimo',
                                'auth', 'profile', 'env']):
                            config_files.append(f)

    # 从所有发现的文件中提取 URL
    for path in config_files:
        urls = extract_urls_from_file(path)
        for key, url in urls.items():
            if url not in all_urls.values():
                all_urls[key] = url

    # 环境变量
    env_urls = extract_urls_from_env()
    for key, url in env_urls.items():
        if url not in all_urls.values():
            all_urls[key] = url

    return all_urls


def classify_url(url: str) -> str:
    """根据 URL 特征分类环境归属"""
    u = url.lower()
    if 'anthropic' in u or 'longcat' in u or 'claude' in u:
        return 'claude'
    if 'openai' in u or '127.0.0.1:10100' in u or 'codex' in u:
        return 'codex'
    if 'grok' in u or 'x.ai' in u:
        return 'grok'
    if 'kimi' in u or 'moonshot' in u:
        return 'kimi'
    if 'mimo' in u:
        return 'mimo'
    return 'unknown'


def probe_url(url: str) -> dict:
    """探测单个 URL 的可达性"""
    start = time.time()
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "env-doctor/0.2")
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
        latency = round(time.time() - start, 3)
        return {
            "url": url,
            "http_code": str(resp.status),
            "latency_s": latency,
            "status": "ok",
        }
    except urllib.error.HTTPError as e:
        latency = round(time.time() - start, 3)
        return {
            "url": url,
            "http_code": str(e.code),
            "latency_s": latency,
            "status": "ok" if e.code < 500 else "degraded",
        }
    except Exception as e:
        latency = round(time.time() - start, 3)
        return {
            "url": url,
            "http_code": "000",
            "latency_s": latency,
            "status": "unreachable",
            "error": str(e)[:100],
        }


def main():
    # 解析参数
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--timeout' and i < len(sys.argv) - 1:
            global TIMEOUT
            TIMEOUT = int(sys.argv[i + 1])

    # 发现 URL
    urls = discover_all_urls()

    # 探测
    results = []
    for source_key, url in urls.items():
        result = probe_url(url)
        result['source'] = source_key
        result['env_classified'] = classify_url(url)
        results.append(result)

    report = {
        "probe_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timeout_sec": TIMEOUT,
        "url_count": len(results),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
PYEOF
