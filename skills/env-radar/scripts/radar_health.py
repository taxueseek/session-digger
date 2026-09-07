#!/usr/bin/env python3
"""radar_health.py — 健康雷达：依赖列表、过期检测、缺失配置。"""
import json
import re
import time
from pathlib import Path

from radar_common import compare_versions, log, safe_read_text


class HealthScanner:
    """健康雷达：依赖列表、过期检测、缺失配置"""

    def __init__(self, root: Path, files: list[dict]):
        self.root = root
        self.files = files

    @staticmethod
    def _dep_record(name: str, version: str, dep_type: str,
                    package_manager: str, file_source: str) -> dict:
        """构造依赖记录（统一字段形状）"""
        return {
            'name': name,
            'version': version,
            'latest_version': None,
            'is_outdated': False,
            'dep_type': dep_type,
            'package_manager': package_manager,
            'file_source': file_source,
        }

    def _parse_package_json(self, filepath: Path) -> list[dict]:
        """解析 package.json 依赖"""
        deps = []
        text = safe_read_text(filepath)
        if not text:
            return deps
        try:
            pkg = json.loads(text)
        except json.JSONDecodeError:
            return deps

        for dep_type, key in [
            ('runtime', 'dependencies'),
            ('dev', 'devDependencies'),
            ('peer', 'peerDependencies'),
            ('optional', 'optionalDependencies'),
        ]:
            section = pkg.get(key, {})
            for name, version in section.items():
                deps.append(self._dep_record(
                    name, version, dep_type, 'npm',
                    str(filepath.relative_to(self.root)),
                ))
        return deps

    def _parse_requirements_txt(self, filepath: Path) -> list[dict]:
        """解析 requirements.txt"""
        deps = []
        text = safe_read_text(filepath)
        if not text:
            return deps
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            # 解析 name==version 或 name>=version 或 name~=version
            match = re.match(r'^([a-zA-Z0-9_-]+)\s*([><=!~]+)?\s*(.*)?$', line)
            if match:
                version = (match.group(3) or '').split(';')[0].strip()  # 去掉环境标记
                deps.append(self._dep_record(
                    match.group(1), version, 'runtime', 'pip',
                    str(filepath.relative_to(self.root)),
                ))
        return deps

    def _parse_pyproject_toml(self, filepath: Path) -> list[dict]:
        """简单解析 pyproject.toml 依赖（不依赖 toml 库）"""
        deps = []
        text = safe_read_text(filepath)
        if not text:
            return deps

        in_deps = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in ('[project.dependencies]', '[tool.poetry.dependencies]'):
                in_deps = True
                continue
            if stripped.startswith('[') and 'dependencies' not in stripped:
                in_deps = False
                continue
            if in_deps and '=' in stripped:
                # 简单解析 name = "version"
                match = re.match(r'^([a-zA-Z0-9_-]+)\s*=\s*["\']?([^"\'\s,]+)', stripped)
                if match:
                    deps.append(self._dep_record(
                        match.group(1), match.group(2), 'runtime', 'pip',
                        str(filepath.relative_to(self.root)),
                    ))
        return deps

    def _parse_cargo_toml(self, filepath: Path) -> list[dict]:
        """简单解析 Cargo.toml 依赖"""
        deps = []
        text = safe_read_text(filepath)
        if not text:
            return deps

        in_deps = False
        dep_type = 'runtime'
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in ('[dependencies]', '[dev-dependencies]'):
                in_deps = True
                dep_type = 'dev' if 'dev' in stripped else 'runtime'
                continue
            if stripped.startswith('[') and 'dependencies' not in stripped:
                in_deps = False
                continue
            if in_deps and '=' in stripped:
                match = re.match(r'^([a-zA-Z0-9_-]+)\s*=\s*["\']?([^"\'\s,]+)', stripped)
                if match:
                    deps.append(self._dep_record(
                        match.group(1), match.group(2), dep_type, 'cargo',
                        str(filepath.relative_to(self.root)),
                    ))
        return deps

    def _parse_go_mod(self, filepath: Path) -> list[dict]:
        """解析 go.mod"""
        deps = []
        text = safe_read_text(filepath)
        if not text:
            return deps

        in_require = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('require ('):
                in_require = True
                continue
            if in_require and stripped == ')':
                in_require = False
                continue
            if in_require or stripped.startswith('require '):
                # 解析 module version
                match = re.match(r'(?:require\s+)?([a-zA-Z0-9./_-]+)\s+(v[0-9.a-z-]+)', stripped)
                if match:
                    deps.append(self._dep_record(
                        match.group(1), match.group(2), 'runtime', 'go',
                        str(filepath.relative_to(self.root)),
                    ))
        return deps

    def _check_outdated(self, deps: list[dict]) -> None:
        """检查依赖是否过期（使用本地缓存，避免频繁网络请求）"""
        # 尝试使用 requests，否则跳过
        try:
            import requests as req_lib
            has_requests = True
        except ImportError:
            has_requests = False

        # 本地缓存
        cache_path = Path.home() / '.claude' / '.env-radar' / 'dep-cache.json'
        cache: dict = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                cache = {}

        now = time.time()
        cache_ttl = 3600 * 6  # 6 小时缓存

        for dep in deps:
            name = dep['name']
            pm = dep['package_manager']
            cache_key = f"{pm}:{name}"

            # 检查缓存
            if cache_key in cache:
                cached = cache[cache_key]
                if now - cached.get('ts', 0) < cache_ttl:
                    dep['latest_version'] = cached.get('latest')
                    continue

            # 网络请求
            if not has_requests:
                continue

            try:
                if pm == 'npm':
                    resp = req_lib.get(
                        f'https://registry.npmjs.org/{name}/latest',
                        timeout=5
                    )
                    if resp.status_code == 200:
                        latest = resp.json().get('version', '')
                        dep['latest_version'] = latest
                        cache[cache_key] = {'latest': latest, 'ts': now}
                elif pm == 'pip':
                    resp = req_lib.get(
                        f'https://pypi.org/pypi/{name}/json',
                        timeout=5
                    )
                    if resp.status_code == 200:
                        latest = resp.json().get('info', {}).get('version', '')
                        dep['latest_version'] = latest
                        cache[cache_key] = {'latest': latest, 'ts': now}
            except Exception:
                pass

        # 保存缓存
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
        except OSError:
            pass

        # 判断是否过期（语义版本比较，避免 "1.2.3" < "1.10.0" 这类字符串误判）
        for dep in deps:
            if dep['latest_version'] and dep['version']:
                current = re.sub(r'^[\^~>=<]+', '', dep['version']).strip()
                latest = dep['latest_version'].strip()
                if not current or not latest:
                    continue
                outdated = compare_versions(current, latest) < 0
                if outdated:
                    dep['is_outdated'] = True

    def _check_missing_config(self) -> list[dict]:
        """检查缺失的配置"""
        conventions = []

        # .env.example vs .env
        env_example = self.root / '.env.example'
        env_file = self.root / '.env'
        if env_example.exists() and env_file.exists():
            example_vars = set()
            for line in safe_read_text(env_example).splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    example_vars.add(line.split('=')[0].strip())

            env_vars = set()
            for line in safe_read_text(env_file).splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    env_vars.add(line.split('=')[0].strip())

            missing = example_vars - env_vars
            if missing:
                conventions.append({
                    'category': 'health',
                    'name': 'missing_env_vars',
                    'value': str(len(missing)),
                    'count': len(missing),
                    'sample_files': json.dumps(list(missing)[:10], ensure_ascii=False),
                })

        # CI 配置检查
        ci_files = {
            '.github/workflows': 'github_actions',
            '.gitlab-ci.yml': 'gitlab_ci',
            '.circleci': 'circleci',
            'Jenkinsfile': 'jenkins',
            '.travis.yml': 'travis',
        }
        has_ci = any((self.root / cf).exists() for cf in ci_files)
        if not has_ci:
            conventions.append({
                'category': 'health',
                'name': 'missing_ci',
                'value': 'true',
                'count': 1,
                'sample_files': '[]',
            })

        return conventions

    def scan(self) -> tuple[list[dict], list[dict]]:
        """执行健康扫描，返回 (dependencies, conventions)"""
        log("分析依赖健康...")

        deps = []

        # 查找并解析依赖文件
        for file_info in self.files:
            fp = Path(file_info['path'])
            fname = fp.name

            if fname == 'package.json':
                deps.extend(self._parse_package_json(fp))
            elif fname == 'requirements.txt':
                deps.extend(self._parse_requirements_txt(fp))
            elif fname == 'pyproject.toml':
                deps.extend(self._parse_pyproject_toml(fp))
            elif fname == 'Cargo.toml':
                deps.extend(self._parse_cargo_toml(fp))
            elif fname == 'go.mod':
                deps.extend(self._parse_go_mod(fp))

        log(f"发现 {len(deps)} 个依赖")

        # 过期检测（仅对前 20 个依赖做网络请求，避免太慢）
        if deps:
            self._check_outdated(deps[:20])

        # 缺失配置检查
        conventions = self._check_missing_config()

        return deps, conventions
