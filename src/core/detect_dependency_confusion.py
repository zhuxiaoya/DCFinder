import os
import sys
import re
import requests
import json
from bs4 import BeautifulSoup
import fnmatch
import pip_requirements_parser
import toml
import dparse2
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import analysis_setup_py
from configparser import ConfigParser
from queue import Queue
from urllib.parse import urljoin
from packaging.version import Version, parse, InvalidVersion
from packaging.specifiers import SpecifierSet
import zipfile,tarfile
from pathlib import Path
import importlib_metadata
from email.parser import Parser
from packaging.version import parse as parse_version
try:
    from zipfile import Path as ZipPath
except ImportError:
    from zipp import Path as ZipPath
import argparse
import json
current_script_path = os.path.realpath(__file__)
current_script_dir = os.path.dirname(current_script_path)
download_dir=os.path.join(current_script_dir,'private_package')
# download_dir='/home/lab314/zxy/Project_progress/dependncy_confusion/private_package'
INDEX_LOOKUP_TIMEOUT = 60
PYPI_JSON_BASE = "https://pypi.org/pypi"  # 使用 PyPI 官方源
_package_index_cache = {}
_pypi_requires_dist_cache = {}

# 跟踪每个包被请求时指定的 extras（如 cuda-toolkit[cublas,...] → {cublas, cudart, ...}）
# 用于在 BFS 展开该包时正确评估 extra == 环境标记
_package_requested_extras = {}


def convert_sets_to_lists(data):
    if isinstance(data, dict):
        return {key: convert_sets_to_lists(value) for key, value in data.items()}
    elif isinstance(data, set):
        return list(data)
    else:
        return data

INDEX_URL_SCAN_SKIP_DIRS = {
    '.git', '.hg', '.svn', '.venv', 'venv', 'env', '__pycache__',
    'node_modules', '.mypy_cache', '.pytest_cache', 'dist',
    'docs', '.github', 'tests', 'test',
}

INDEX_URL_SCAN_FILE_PATTERNS = (
    '*requirement*.txt',
    '*requirement*.pip',
    '*requirement*.in',
    '*requires.txt',
    '*requirements/*.txt',
    '*requirements/*.pip',
    '*requirements/*.in',
    '*reqs.txt',
    'pyproject.toml',
    'setup.py',
    'setup.cfg',
    'Pipfile',
    'Dockerfile',
    'Dockerfile.*',
    'dockerfile',
    '*.inc.md',
)


def normalize_private_index_url(raw_url):
    """清理从文本中提取的 index URL（去掉引号、反引号、尾部标点）。"""
    url = (raw_url or '').strip().strip('\'"').rstrip('`,.;)]}>')
    if url.endswith('`'):
        url = url[:-1]
    return url


def is_plausible_pip_index_url(url):
    """
    过滤明显不是 pip index 的 URL：
    - shell 片段 / 环境变量占位符
    - 数据文件、wheel、文档等
    """
    if not url or not url.startswith(('http://', 'https://')):
        return False
    if any(ch in url for ch in ('$', '(', '{', '`', '\n', ' ')):
        return False
    path = url.split('?', 1)[0].split('#', 1)[0].lower()
    bad_suffixes = (
        '.jsonl', '.json', '.whl', '.tar.gz', '.zip', '.md', '.py', '.sh',
        '.yaml', '.yml', '.toml', '.csv',
    )
    for suffix in bad_suffixes:
        if path.endswith(suffix):
            return False
    return True


def should_scan_file_for_index_urls(file_path, rel_path):
    name = os.path.basename(file_path)
    if name in {'pyproject.toml', 'setup.py', 'setup.cfg', 'Pipfile', 'dockerfile'}:
        return True
    if name.startswith('Dockerfile'):
        return True
    return matches_patterns(rel_path, INDEX_URL_SCAN_FILE_PATTERNS) or matches_patterns(
        name, INDEX_URL_SCAN_FILE_PATTERNS
    )


def scan_private_urls(directory):
    """
    从依赖相关配置文件中提取非 PyPI 的 index URL
    （--index-url / --extra-index-url / [[tool.poetry.source]]）。
    不扫描 docs/examples/tests 等目录，避免文档示例与 CLI 参数误匹配。
    """
    url_patterns = {
        "index_url": re.compile(
            r"(?:--index-url|-i)\s*=?\s*(https?://[^\s'\"`]+)",
            re.IGNORECASE,
        ),
        "extra_index_url": re.compile(
            r"--extra-index-url\s*=?\s*(https?://[^\s'\"`]+)",
            re.IGNORECASE,
        ),
    }

    found_urls = {
        "index_url": set(),
        "extra_index_url": set(),
    }
    file_private_sources = {}  # {filename: {url_type: [urls]}}
    if not os.path.exists(directory):
        print(f"not exits: ")
        return set(), {}

    for root, dirs, files in os.walk(directory):
        dirs[:] = [
            d for d in dirs
            if d not in INDEX_URL_SCAN_SKIP_DIRS and not d.startswith('.')
        ]
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, directory).replace('\\', '/')
            if not should_scan_file_for_index_urls(file_path, rel_path):
                continue
            try:
                with open(file_path, "r", encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    for url_type, pattern in url_patterns.items():
                        matches = pattern.findall(content)
                        for match in matches:
                            url = normalize_private_index_url(match)
                            if is_plausible_pip_index_url(url):
                                found_urls[url_type].add(url)
                                file_private_sources.setdefault(rel_path, {}).setdefault(url_type, []).append(url)

                    if file_path.endswith('pyproject.toml'):
                        try:
                            if sys.version_info >= (3, 11):
                                import tomllib
                            else:
                                import tomli as tomllib
                            data = tomllib.loads(content)
                            sources = data.get('tool', {}).get('poetry', {}).get('source', [])
                            for src in sources:
                                if 'url' in src:
                                    url = normalize_private_index_url(src['url'])
                                    if is_plausible_pip_index_url(url):
                                        found_urls['extra_index_url'].add(url)
                                        file_private_sources.setdefault(rel_path, {}).setdefault('poetry.source', []).append(url)
                        except Exception:
                            pass

                    if os.path.basename(file_path).lower() == 'pipfile':
                        try:
                            if sys.version_info >= (3, 11):
                                import tomllib
                            else:
                                import tomli as tomllib
                            data = tomllib.loads(content)
                            sources = data.get('source', [])
                            for src in sources:
                                if 'url' in src:
                                    url = normalize_private_index_url(src['url'])
                                    if is_plausible_pip_index_url(url):
                                        found_urls['extra_index_url'].add(url)
                                        file_private_sources.setdefault(rel_path, {}).setdefault('pipfile.source', []).append(url)
                        except Exception:
                            pass

            except Exception:
                continue

    MIRROR_DOMAINS = (
        'tuna.tsinghua.edu.cn', 'mirrors.aliyun.com', 'mirrors.cloud.tencent.com',
        'mirrors.ustc.edu.cn', 'mirrors.huaweicloud.com', 'mirrors.163.com',
        'pypi.douban.com',
    )
    private_urls = {
        v for value in found_urls.values() for v in value
        if v != 'https://pypi.org/pypi'
        and not any(d in v for d in MIRROR_DOMAINS)
    }
    return private_urls, file_private_sources


def _write_private_sources(output_path, project_directory, file_private_sources):
    """写出 private_sources.txt，记录每个配置文件中的私有源 URL。"""
    MIRRORS = ('tuna.tsinghua.edu.cn', 'mirrors.aliyun.com', 'mirrors.cloud.tencent.com',
               'mirrors.ustc.edu.cn', 'mirrors.huaweicloud.com', 'mirrors.163.com',
               'pypi.douban.com')

    def _is_private(url):
        return url != 'https://pypi.org/pypi' and not any(d in url for d in MIRRORS)

    os.makedirs(output_path, exist_ok=True)
    project_name = os.path.basename(project_directory.rstrip('/'))

    # 过滤掉 PyPI 及镜像 URL，只保留真正的私有源
    filtered = {}
    for rel_path, url_dict in file_private_sources.items():
        filtered_urls = {}
        for url_type, urls in url_dict.items():
            private_urls_list = [u for u in urls if _is_private(u)]
            if private_urls_list:
                filtered_urls[url_type] = private_urls_list
        if filtered_urls:
            filtered[rel_path] = filtered_urls

    with open(os.path.join(output_path, 'private_sources.txt'), 'w', encoding='utf-8') as f:
        f.write(f"# 私有源 (非 PyPI) 配置文件扫描结果 — {project_name}\n")
        config_count = len(filtered)
        f.write(f"# 共发现 {config_count} 个配置文件含私有源\n\n")
        if config_count == 0:
            f.write("# (无)\n")
        else:
            for filename in sorted(filtered.keys()):
                f.write(f"[{filename}]\n")
                for url_type in sorted(filtered[filename].keys()):
                    for url in filtered[filename][url_type]:
                        f.write(f"  {url_type}: {url}\n")
                f.write("\n")


dependencies_patterns = (
    '*requirement*.txt',
    '*requirement*.pip',
    '*requirement*.in',
    '*requires.txt',
    '*requirements/*.txt',
    '*requirements/*.pip',
    '*requirements/*.in',
    '*reqs.txt',
    '*pyproject.toml',
    '*setup.py',
    '*setup.cfg',
    '*Pipfile',
    '*/requirements/*.txt',
    '*/requirements/*.pip',
    '*/requirements/*.in',
    '*/pyproject.toml',
    '*/setup.py',
    '*/setup.cfg',
    '*/Pipfile',
    '**/*requirement*.txt',
    '**/*requirement*.pip',
    '**/*requirement*.in',
    '**/*requires.txt',
    '**/requirements/*.txt',
    '**/requirements/*.pip',
    '**/requirements/*.in',
    '**/reqs.txt',
    '**/pyproject.toml',
    '**/setup.py',
    '**/setup.cfg',
    '**/Pipfile',
)
def matches_patterns(filename, patterns):
    """检查文件名是否与给定模式匹配（支持 ** 递归通配）。"""
    for pattern in patterns:
        if '**' in pattern:
            parts = pattern.split('/')
            if match_recursive(filename, parts):
                return True
        elif fnmatch.fnmatch(filename, pattern):
            return True
    return False

def match_recursive(path, pattern_parts):
    """递归匹配路径模式（支持 **）。"""
    path_parts = path.split('/')
    i = j = 0
    
    while i < len(path_parts) and j < len(pattern_parts):
        if pattern_parts[j] == '**':
            if j == len(pattern_parts) - 1:
                return True
            j += 1
            while i < len(path_parts):
                if match_recursive('/'.join(path_parts[i:]), pattern_parts[j:]):
                    return True
                i += 1
            return False
        elif fnmatch.fnmatch(path_parts[i], pattern_parts[j]):
            i += 1
            j += 1
        else:
            return False
    
    return i == len(path_parts) and j == len(pattern_parts)

def dependency_dict(name, specifier='', raw_requirement='', source_file='', dependency_type='runtime'):
    """
    创建依赖包字典，增加 dependency_type 字段记录依赖类型：
    - runtime: 运行时依赖（install_requires / dependencies）
    - dev: 开发依赖（dev-dependencies / dev-packages）
    - test: 测试依赖（tests_require）
    - setup: 构建依赖（setup_requires）
    - extra:<name>: 可选依赖（optional-dependencies / extras_require）
    """
    return {
        'name': canonicalize_name(name),
        'specifier': str(specifier) if specifier else '',
        'raw_requirement': raw_requirement or name,
        'source_file': source_file,
        'dependency_type': dependency_type,
    }


def merge_specifiers(left, right):
    left = (left or '').strip()
    right = (right or '').strip()
    if not left:
        return right
    if not right:
        return left
    if left == right:
        
        return left
    if '==' in left or '===' in left:
        return left
    if '==' in right or '===' in right:
        return right
    return f'{left},{right}'


def merge_dependency_declarations(deps):
    merged = {}
    for dep in deps:
        name = dep['name']
        if name not in merged:
            merged[name] = dict(dep)
            continue
        merged[name]['specifier'] = merge_specifiers(
            merged[name].get('specifier', ''),
            dep.get('specifier', ''),
        )
        merged[name]['raw_requirement'] = (
            f"{merged[name]['raw_requirement']} ; {dep['raw_requirement']}"
        )
    return list(merged.values())


def dependency_from_requirement_string(req_string, source_file=''):
    req_string = (req_string or '').strip()
    if not req_string or req_string.startswith('#'):
        return None
    if 'https://github.com' in req_string:
        return None
    if '--index-url' in req_string or '--extra-index-url' in req_string:
        parts = (
            req_string.split('--index-url')
            if '--index-url' in req_string
            else req_string.split('--extra-index-url')
        )
        req_string = parts[0].strip()
    try:
        req = Requirement(req_string)
    except Exception:
        return None
    if not req.name:
        return None
    return dependency_dict(
        name=req.name,
        specifier=str(req.specifier),
        raw_requirement=req_string,
        source_file=source_file,
    )


def poetry_spec_to_pip_specifier(spec_str):
    """Convert a Poetry version specifier to a pip-compatible specifier string."""
    spec_str = (spec_str or '').strip()
    if not spec_str or spec_str == '*':
        return ''
    # Poetry bare version (e.g. "4.2.20") means exact pin → "==4.2.20"
    if spec_str[0].isdigit():
        return f'=={spec_str}'
    # Poetry caret (e.g. "^4.2.20") → ">=4.2.20,<5.0.0"
    if spec_str.startswith('^'):
        ver = spec_str[1:].strip()
        parts = ver.split('.')
        if len(parts) >= 1 and parts[0] != '0':
            upper = str(int(parts[0]) + 1) + '.0.0'
        elif len(parts) >= 2 and parts[1] != '0':
            upper = parts[0] + '.' + str(int(parts[1]) + 1) + '.0'
        elif len(parts) >= 3:
            upper = parts[0] + '.' + parts[1] + '.' + str(int(parts[2]) + 1)
        else:
            upper = ''
        return f'>={ver},<{upper}' if upper else f'>={ver}'
    # Poetry tilde (e.g. "~4.2.20") → ">=4.2.20,<4.3.0"
    if spec_str.startswith('~'):
        ver = spec_str[1:].strip()
        parts = ver.split('.')
        if len(parts) >= 2:
            upper = parts[0] + '.' + str(int(parts[1]) + 1) + '.0'
        else:
            upper = ''
        return f'>={ver},<{upper}' if upper else f'>={ver}'
    # Already pip-style (e.g. ">=4.2.20", "!=1.0", etc.)
    return spec_str


def dependency_from_poetry_spec(package, spec_str, source_file=''):
    spec_str = (spec_str or '').strip()
    if not spec_str or spec_str == '*':
        return dependency_dict(package, '', package, source_file)
    pip_spec = poetry_spec_to_pip_specifier(spec_str)
    req_str = f'{package}{pip_spec}' if pip_spec else package
    dep = dependency_from_requirement_string(req_str, source_file=source_file)
    if dep:
        return dep
    return dependency_dict(package, pip_spec, req_str, source_file)


def _parse_req_line_with_inline_option(line, source_file=''):
    """Fallback 解析带内联 --extra-index-url/--index-url 的行。

    pip_requirements_parser 会丢弃含内联选项的行（如
    ``torch>=1.9.0 --extra-index-url https://...``），本函数通过正则
    剥离选项部分后单独解析包名+版本。
    """
    if not line or line.startswith('#'):
        return None
    if 'https://github.com' in line:
        return None
    if '--index-url' in line or '--extra-index-url' in line:
        line = re.sub(r'--extra-index-url\s+https?://\S+', '', line).strip()
        line = re.sub(r'--index-url\s+https?://\S+', '', line).strip()
    if not line:
        return None
    try:
        req = Requirement(line)
    except Exception:
        return None
    if not req.name:
        return None
    return dependency_dict(
        name=req.name,
        specifier=str(req.specifier),
        raw_requirement=line,
        source_file=source_file,
    )


def get_requirements_txt_dependencies(location, visited=None):
    if visited is None:
        visited = set()
    if location in visited:
        return []
    visited.add(location)

    req_file = pip_requirements_parser.RequirementsFile.from_file(filename=location)

    dependent_packages = []
    req_dir = os.path.dirname(location)
    parsed_raw_lines = set()

    if req_file and req_file.requirements:
        for req in req_file.requirements:
            if not req.name:
                line = getattr(req, 'line', '') or str(req)
                line = line.strip()
                parsed_raw_lines.add(line)
                if line.startswith('-r ') or line.startswith('--requirement '):
                    included_path = line.split(' ', 1)[1].strip()
                    if not os.path.isabs(included_path):
                        included_path = os.path.join(req_dir, included_path)
                    if os.path.exists(included_path):
                        dependent_packages.extend(get_requirements_txt_dependencies(included_path, visited))
                else:
                    # fallback：pip_requirements_parser 返回 name=None 的行
                    # （如带内联 --extra-index-url 的行）
                    dep = _parse_req_line_with_inline_option(line, location)
                    if dep:
                        dependent_packages.append(dep)
                continue

            raw_line = getattr(req, 'line', '') or str(req)
            parsed_raw_lines.add(raw_line.strip())
            dependent_packages.append(dependency_dict(
                name=req.name,
                specifier=str(getattr(req, 'specifier', '') or ''),
                raw_requirement=raw_line,
                source_file=location,
            ))

    # 第二轮：扫描原始文件中 pip_requirements_parser 完全丢弃的行
    try:
        with open(location, encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if line in parsed_raw_lines:
                    continue
                # 跳过纯选项行（如 --extra-index-url https://... 单独一行）
                if line.startswith('--') or line.startswith('-i '):
                    continue
                dep = _parse_req_line_with_inline_option(line, location)
                if dep:
                    dependent_packages.append(dep)
    except (OSError, UnicodeDecodeError):
        pass

    return dependent_packages

def get_pyproject_toml_dependencies(location):
    """
    从 pyproject.toml 提取直接依赖包（与 v3 保持一致，但记录依赖类型）。
    
    设计说明：
    1. 不解析 poetry.lock 文件（包含所有间接依赖，会导致误报）
    2. 提取所有类型依赖，但记录 dependency_type 字段：
       - runtime: 主依赖（tool.poetry.dependencies / project.dependencies）
       - dev: 开发依赖（tool.poetry.dev-dependencies）
       - extra:<name>: 可选依赖（project.optional-dependencies）
    """
    dependent_packages = []
    package_data = toml.load(location, _dict=dict)
    tool_name = package_data.get('tool', {})
    
    if 'poetry' in tool_name:
        project_data = tool_name['poetry']
        packages = project_data.get('dependencies', {})
        for package, value in packages.items():
            if package.lower() == 'python':
                continue
            if isinstance(value, str):
                spec_str = value
            elif isinstance(value, dict):
                spec_str = value.get('version', '')
            else:
                spec_str = str(value)
            dep = dependency_from_poetry_spec(package, spec_str, source_file=location)
            if dep:
                dep['dependency_type'] = 'runtime'
                dependent_packages.append(dep)

        dev_packages = project_data.get('dev-dependencies', {})
        for package, value in dev_packages.items():
            if package.lower() == 'python':
                continue
            if isinstance(value, str):
                spec_str = value
            elif isinstance(value, dict):
                spec_str = value.get('version', '')
            else:
                spec_str = str(value)
            dep = dependency_from_poetry_spec(package, spec_str, source_file=location)
            if dep:
                dep['dependency_type'] = 'dev'
                dependent_packages.append(dep)

    elif 'project' in package_data:
        project_data = package_data.get("project")
        packages = project_data.get("dependencies", [])
        for req in packages:
            dep = dependency_from_requirement_string(req, source_file=location)
            if dep:
                dep['dependency_type'] = 'runtime'
                dependent_packages.append(dep)

        optional_packages = project_data.get("optional-dependencies", {})
        for dep_type, deps in optional_packages.items():
            if isinstance(deps, list):
                flat_deps = deps
            else:
                flat_deps = [deps]
            for req in flat_deps:
                dep = dependency_from_requirement_string(req, source_file=location)
                if dep:
                    dep['dependency_type'] = f'extra:{dep_type}'
                    dependent_packages.append(dep)

    return dependent_packages

def get_Pipfile_dependencies(location):
    """
    从 Pipfile 提取直接依赖包（与 v3 保持一致，但记录依赖类型）。
    
    设计说明：
    1. 不解析 Pipfile.lock 文件（包含所有间接依赖，会导致误报）
    2. 提取所有类型依赖，但记录 dependency_type 字段：
       - runtime: 主依赖（[packages]）
       - dev: 开发依赖（[dev-packages]）
    """
    dependent_packages = []
    content = toml.load(location)
    
    packages = content.get('packages', {})
    for package_name, package_info in packages.items():
        if isinstance(package_info, dict):
            spec_str = package_info.get('version', '*')
        else:
            spec_str = str(package_info)
        if spec_str in {'*', ''}:
            req_line = package_name
        elif spec_str.startswith(('=', '<', '>', '!', '~')):
            req_line = f'{package_name}{spec_str}'
        else:
            req_line = f'{package_name}=={spec_str}'
        dep = dependency_from_requirement_string(req_line, source_file=location)
        if dep:
            dep['dependency_type'] = 'runtime'
            dependent_packages.append(dep)

    dev_packages = content.get('dev-packages', {})
    for package_name, package_info in dev_packages.items():
        if isinstance(package_info, dict):
            spec_str = package_info.get('version', '*')
        else:
            spec_str = str(package_info)
        if spec_str in {'*', ''}:
            req_line = package_name
        elif spec_str.startswith(('=', '<', '>', '!', '~')):
            req_line = f'{package_name}{spec_str}'
        else:
            req_line = f'{package_name}=={spec_str}'
        dep = dependency_from_requirement_string(req_line, source_file=location)
        if dep:
            dep['dependency_type'] = 'dev'
            dependent_packages.append(dep)

    return dependent_packages

def get_setup_cfg_dependencies(location):
    """
    从 setup.cfg 提取直接依赖包（与 v3 保持一致，但记录依赖类型）。
    
    设计说明：
    1. 提取所有类型依赖，但记录 dependency_type 字段：
       - runtime: 安装依赖（install_requires）
       - test: 测试依赖（tests_require）
       - setup: 构建依赖（setup_requires）
       - extra:<name>: 可选依赖（options.extras_require）
    """
    dependencies = []
    parser = ConfigParser()
    with open(location) as f:
        parser.read_file(f)
    for section in parser.values():
        if section.name == 'options':
            scope_by_sub_section = {
                "install_requires": "runtime",
                "tests_require": "test",
                "setup_requires": "setup",
                "python_requires": "python",
            }
            for sub_section, dep_type in scope_by_sub_section.items():
                if sub_section not in section:
                    continue
                if dep_type == "python":
                    continue
                content = section.get(sub_section, "").strip('\n').split('\n')
                for req in content:
                    dep = dependency_from_requirement_string(req, source_file=location)
                    if dep:
                        dep['dependency_type'] = dep_type
                        dependencies.append(dep)

        if section.name == "options.extras_require":
            for sub_section in section:
                content = section.get(sub_section, "").strip('\n').split('\n')
                for req in content:
                    dep = dependency_from_requirement_string(req, source_file=location)
                    if dep:
                        dep['dependency_type'] = f'extra:{sub_section}'
                        dependencies.append(dep)

    return dependencies


# 平台特定关键词：文件名包含这些关键词的 requirements 文件大概率不是当前平台的依赖
# （如 requirements_macos.txt, requirements_linux_rocm.txt, requirements-win-*.txt 等）
# 注意：CUDA/GPU 相关不在此列表，因为 CUDA 是大多数开发机器的默认环境
_PLATFORM_KEYWORDS = (
    'macos', 'macosx', 'mac_os', 'mac-os', 'darwin', 'apple',
    'windows', 'win32', 'win64', 'win-', '_win',
    'linux', 'ubuntu', 'debian', 'centos',
    'rocm', 'ipex', 'runpod', 'amd', 'dml', 'directml',
    'colab', 'kaggle',
    'arm64', 'aarch64', 'x86_64', 'amd64',
    'nightly',
)

# 子目录扫描时跳过的目录名（不跳过 examples/tests/docs — 它们可能包含有效的 sub-project 依赖）
_SUBDIR_SCAN_SKIP = {
    '.git', '.hg', '.svn', '__pycache__', '.mypy_cache', '.pytest_cache',
    '.venv', 'venv', 'env', '.env', 'node_modules',
    'dist', 'build', '.tox', '.eggs', '*.egg-info',
    '.github', '.circleci',
}


def _is_platform_specific(filename):
    """判断 requirements 文件名是否明确指向特定平台/环境。"""
    lower = filename.lower()
    for kw in _PLATFORM_KEYWORDS:
        if kw in lower:
            return True
    return False


def _collect_config_files_from_dir(root):
    """Collect dependency config files from a directory (non-recursive)."""
    result = []
    try:
        entries = os.listdir(root)
    except PermissionError:
        return result

    for fname in entries:
        fpath = os.path.join(root, fname)
        if not os.path.isfile(fpath):
            continue
        lower = fname.lower()
        if lower in {'pyproject.toml', 'setup.py', 'setup.cfg', 'pipfile'}:
            result.append(fpath)
        elif lower.startswith('requirements') and lower.endswith('.txt'):
            if not _is_platform_specific(fname):
                result.append(fpath)
        elif lower.startswith('requirements') and (lower.endswith('.pip') or lower.endswith('.in')):
            if not _is_platform_specific(fname):
                result.append(fpath)
    return result


def _collect_subdir_config_files(directory):
    """Recursively scan subdirectories for dependency config files."""
    result = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SUBDIR_SCAN_SKIP
            and not d.startswith('.')
            and not d.endswith('.egg-info')
        ]
        if os.path.samefile(dirpath, directory):
            continue
        for fname in filenames:
            lower = fname.lower()
            if lower in {'pyproject.toml', 'setup.py', 'setup.cfg', 'pipfile'}:
                result.append(os.path.join(dirpath, fname))
            elif lower.startswith('requirements') and lower.endswith(('.txt', '.pip', '.in')):
                if not _is_platform_specific(fname):
                    result.append(os.path.join(dirpath, fname))
    return result


def get_dependency(directory, output_path=None):
    """
    扫描项目根目录及子目录的配置文件并提取直接依赖包（与 v3 保持一致，但记录依赖类型）。

    设计说明：
    1. 根目录：扫描所有标准配置文件（pyproject.toml, setup.py, setup.cfg, Pipfile,
       requirements*.txt 等），但跳过明确指向特定平台的 requirements 文件
       （如 requirements_macos.txt, requirements_linux_rocm.txt 等）。
    2. 子目录：递归扫描子目录中的 pyproject.toml 和 requirements*.txt，
       跳过测试、文档、示例等非核心目录。
    3. 支持的配置文件类型：
       - requirements.txt（及其变体：requirements-dev.txt, requirements-test.txt 等）
       - pyproject.toml（PEP 621 标准配置）
       - setup.py（传统 setuptools 配置）
       - setup.cfg（传统 setuptools 配置）
       - Pipfile（pipenv 配置）
    4. 提取所有类型依赖，但记录 dependency_type 字段：
       - runtime: 运行时依赖（install_requires / dependencies）
       - dev: 开发依赖（dev-dependencies / dev-packages / requirements-dev.txt）
       - test: 测试依赖（tests_require / requirements-test.txt）
       - setup: 构建依赖（setup_requires）
       - extra:<name>: 可选依赖（optional-dependencies / extras_require）
    5. 不解析 lock 文件（poetry.lock, Pipfile.lock），因为 lock 文件包含所有间接依赖
       原因：lock 文件包含递归依赖树的所有包，会导致间接依赖被误判为直接依赖

    参数：
        directory: 项目根目录路径
        output_path: 输出目录，若提供则将依赖文件路径及其依赖项写入 {output_path}/file_include_dependency.txt

    返回：
        合并后的直接依赖包列表（去重并合并版本约束，包含 dependency_type 字段）
    """
    dependencies = []
    report_fd = None
    if output_path:
        os.makedirs(output_path, exist_ok=True)
        output_file = os.path.join(output_path, 'file_include_dependency.txt')
        report_fd = open(output_file, 'w', encoding='utf-8')

    # 1. 根目录：收集核心配置文件 + 过滤平台特定 requirements
    root_config_paths = _collect_config_files_from_dir(directory)

    # 2. 子目录：递归收集 pyproject.toml 和 requirements*.txt
    subdir_config_paths = _collect_subdir_config_files(directory)

    all_config_paths = root_config_paths + subdir_config_paths

    for file_path in all_config_paths:
        filename = os.path.basename(file_path)

        if filename.lower().startswith('requirements'):
            file_dependencies = get_requirements_txt_dependencies(file_path)
            req_lower = filename.lower()
            if 'dev' in req_lower:
                dep_type = 'dev'
            elif 'test' in req_lower:
                dep_type = 'test'
            else:
                dep_type = 'runtime'
            for dep in file_dependencies:
                dep['dependency_type'] = dep_type
        elif filename.lower() == 'pyproject.toml':
            file_dependencies = get_pyproject_toml_dependencies(file_path)
        elif filename.lower() == 'pipfile':
            file_dependencies = get_Pipfile_dependencies(file_path)
        elif filename.lower() == 'setup.py':
            file_dependencies = analysis_setup_py.get_setup_py_dependencies(file_path)
        elif filename.lower() == 'setup.cfg':
            file_dependencies = get_setup_cfg_dependencies(file_path)
        else:
            file_dependencies = []

        dependencies.extend(file_dependencies)

        if report_fd is not None:
            report_fd.write(f"# 依赖文件: {file_path}\n")
            for dep in file_dependencies:
                dep_type = dep.get('dependency_type', 'runtime')
                if dep.get('specifier'):
                    report_fd.write(f"{dep['name']} {dep['specifier']}  # type: {dep_type}\n")
                else:
                    report_fd.write(f"{dep['name']}  # type: {dep_type}\n")
            report_fd.write("\n")

    if report_fd is not None:
        report_fd.close()

    return merge_dependency_declarations(dependencies)


def index_package_slug(package_name):
    """PEP 503 simple index 路径中的包名。"""
    return canonicalize_name(package_name).replace('_', '-').lower()


def _requests_session():
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def is_package_on_index(base_url, package_name, timeout=INDEX_LOOKUP_TIMEOUT):
    """
    按需检查某个包是否出现在给定 simple index 上（不拉取整页 index 列表）。
    """
    cache_key = (base_url.rstrip('/'), index_package_slug(package_name))
    if cache_key in _package_index_cache:
        return _package_index_cache[cache_key]

    package_url = urljoin(base_url.rstrip('/') + '/', index_package_slug(package_name) + '/')
    session = _requests_session()
    found = False
    try:
        response = session.get(package_url, timeout=timeout, stream=True)
        if response.status_code != 200:
            found = False
        else:
            chunk = b''
            for part in response.iter_content(chunk_size=8192):
                chunk += part
                if len(chunk) >= 16384:
                    break
            text = chunk.decode('utf-8', errors='ignore').lower()
            found = ('.whl' in text) or ('.tar.gz' in text) or ('href=' in text and 'parent' in text)
    except requests.exceptions.RequestException:
        found = False

    _package_index_cache[cache_key] = found
    return found


def resolve_private_index_url(package_name, private_urls):
    """返回 package 所在的第一个 private index URL，不存在则 None。"""
    for url in private_urls:
        if is_package_on_index(url, package_name):
            return url
    return None


# 从指定 URL 获取包名（全量 listing；大 index 易超时，主流程已改用按需探测）
def fetch_package_names_from_url(url):
    url = normalize_private_index_url(url)
    if not is_plausible_pip_index_url(url):
        return []

    session = _requests_session()

    try:
        response = session.get(url, timeout=INDEX_LOOKUP_TIMEOUT)
        response.raise_for_status()
        html_content = response.text
        soup = BeautifulSoup(html_content, 'html.parser')
        package_names = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            package_name = href.rstrip('/').split(r'/')[-1]
            package_names.append(package_name)
        return list(set(sorted(package_names)))

    except requests.exceptions.RequestException as e:
        print(
            f"Index listing unavailable for {url} ({e}); "
            f"will use per-package lookup instead."
        )
        return []

def public_pipy_checker(packages):
    print("pipy checker")
    pkg_pipy_list={}
    for pkg in packages:
        url = f'{PYPI_JSON_BASE}/{pkg}/json'
        response = requests.get(url)

        if response.status_code == 200:
            package_info = response.json()
            info = package_info.get('info', {})
            author=info.get('author')
            dependencies = info.get('requires_dist')
            releases = package_info['releases']
            a=1

def extract_version(file_name, package_name=None):
    """
    从文件名提取版本号。

    支持多种版本格式：
    - 语义化版本：1.2.3, 0.4.dev1
    - 日期格式版本：20090110, 20231225
    - 混合格式：v1.2.3-20201231

    如果提供了 package_name，会先从文件名剥离包名前缀，
    避免包名中含有数字时被误识别为版本号（如 fido2-0.9.2 → "0.9.2" 而非 "2"）。
    """
    search_str = file_name
    if package_name:
        # 尝试剥离包名前缀: {slug}-{version}...
        slug = canonicalize_name(package_name).replace('_', '-').lower()
        for prefix in (slug, package_name.lower().replace('_', '-')):
            if search_str.lower().startswith(prefix + '-'):
                search_str = search_str[len(prefix) + 1:]
                break

    # 优先匹配: 行首的语义化版本 (必须含至少一个点，或为8位日期)
    version_pattern = r"""
        ^
        v?
        (\d+(?:\.\d+)+(?:\.?(?:dev|a|b|rc)\d+)?)   # 语义化版本 (如 1.2.3, 44.0.1)
        (?:[-.]\d{8})?                                # 可选日期后缀
    """
    m = re.search(version_pattern, search_str, re.VERBOSE)
    if m:
        return m.group(1)

    # fallback: 匹配8位日期版本 (如 20090110) 或纯数字
    fallback = r"""
        (?:
            v?
            (\d+(?:\.\d+)+)              # 必须含至少一个点
            (?:[\.-]?(?:dev|a|b|rc)\d+)?
        )|(
            (\d{8})                       # 8位日期格式
        )
    """
    m2 = re.search(fallback, search_str, re.VERBOSE)
    if m2:
        return next((g for g in m2.groups() if g), None)
    return None

# def get_latest_version(files):
#     """获取最新版本文件（支持日期格式）"""
#     version_records = []
#     for file_name in files:
#         version_str = extract_version(file_name)
#         # print(version_str)
#         if not version_str:
#             continue
#
#         # 解析版本号
#         try:
#             if version_str.isdigit():
#                 # 纯数字版本转换为整数（如20090110 → 20090110）
#                 parsed_version = int(version_str)
#             else:
#                 parsed_version = parse_version(version_str)
#         except (InvalidVersion, ValueError):
#             continue
#
#         version_records.append((parsed_version, file_name))
#
#     if not version_records:
#         return None
#
#     # 自定义排序规则：按版本类型和大小排序
#     version_records.sort(key=lambda x: (
#         isinstance(x[0], int),  # 数字版本（日期）排后面
#         x[0] if isinstance(x[0], int) else str(x[0])
#     ), reverse=True)
#     return version_records[0][1]



def get_latest_version(files):
    """获取最新版本文件（支持语义化版本）"""
    version_records = []
    for file_name in files:
        version_str = extract_version(file_name)
        if not version_str:
            continue
        try:
            parsed_version = parse_version(version_str)
            version_records.append((parsed_version, file_name))
        except InvalidVersion:
            continue

    if not version_records:
        return None

    # ✅ 使用 Version 对象的自然排序
    version_records.sort(key=lambda x: x[0], reverse=True)
    return version_records[0][1]


def _archive_looks_intact(path):
    """判断已下载的 wheel/sdist 是否完整（未被截断）。
    残缺文件会让 zipfile/tarfile 打开失败，从而丢失 Requires-Dist。"""
    try:
        name = path.lower().split('#')[0]
        if name.endswith('.whl') or name.endswith('.zip'):
            return bool(zipfile.is_zipfile(path))
        if name.endswith('.tar.gz'):
            return bool(tarfile.is_tarfile(path))
    except Exception:
        return False
    # 非归档类型默认视为完整
    return True


def download_file(file_url, download_dir):
    """
    下载指定文件到本地目录。

    Args:
        file_url (str): 文件的下载链接。
        download_dir (str): 本地文件存储路径。
    """
    # 获取文件名（保留 simple index href 中的 #sha256=... fragment，以匹配已下载的旧文件，
    # 避免重复下载大文件；parse_metadata 内部会剥离 fragment 再做后缀派发）
    file_name = os.path.basename(file_url)
    file_path = os.path.join(download_dir, file_name)

    # 检查文件是否已存在；若存在但损坏/截断（下载未完成被缓存），删除重下，
    # 否则会用残缺文件毒化缓存，导致 parse_metadata 永久拿不到 Requires-Dist。
    if os.path.exists(file_path):
        if _archive_looks_intact(file_path):
            print(f"File {file_name} already exists. Skipping download.")
            return
        print(f"File {file_name} exists but looks incomplete/corrupt, re-downloading...")
        try:
            os.remove(file_path)
        except OSError:
            pass

    # 下载文件
    print(f"Downloading {file_name} from {file_url}...")
    with requests.get(file_url, stream=True) as response:
        response.raise_for_status()
        with open(file_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
    print(f"Saved {file_name} to {file_path}")

def download_from_pypi_simple(package_name, download_dir, specifier=''):
    """
    从 PyPI simple index 下载包，按版本约束过滤。

    Args:
        package_name (str): 包名称。
        download_dir (str): 本地文件存储目录。
        specifier (str): 版本约束（如 "==2.1.0", ">=2.0" 或 ""）。

    Returns: (filename, version_str) — filename 为下载后的本地文件名，version_str 为选中版本号
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import parse as parse_ver

    spec_str = (specifier or '').strip()
    has_spec = bool(spec_str)

    pypi_simple_url = f"https://pypi.org/simple/{index_package_slug(package_name)}/"
    print(f"  [PyPI fallback] Visiting {pypi_simple_url} (spec={spec_str or 'any'})...")

    session = _requests_session()
    try:
        response = session.get(pypi_simple_url, timeout=INDEX_LOOKUP_TIMEOUT)
        response.raise_for_status()
    except Exception:
        print(f"  [PyPI fallback] Failed to fetch {pypi_simple_url}")
        return None, None

    soup = BeautifulSoup(response.text, 'html.parser')

    files = []
    for link in soup.find_all('a', href=True):
        href = link['href']
        href_clean = href.split('#')[0]
        if href_clean.endswith(('.whl', '.tar.gz', '.zip')):
            files.append(href.rstrip('/'))

    if not files:
        print(f"  [PyPI fallback] No valid files found for {package_name}")
        return None, None

    version_records = []
    for file_name in files:
        actual_file_name = file_name.rstrip('/').split('/')[-1]
        version_str = extract_version(actual_file_name, package_name)
        if not version_str:
            continue
        try:
            parsed = parse_ver(version_str)
            version_records.append((parsed, file_name))
        except InvalidVersion:
            continue

    if not version_records:
        print(f"  [PyPI fallback] No versioned files, taking first available")
        latest_file = files[0]
        file_url = urljoin(pypi_simple_url, latest_file)
        download_file(file_url, download_dir)
        return latest_file.split('/')[-1], None

    if has_spec:
        spec_set = SpecifierSet(spec_str)
        filtered = [(v, fn) for v, fn in version_records if spec_set.contains(v)]
        if filtered:
            version_records = filtered
            print(f"  [PyPI fallback] Spec {spec_str}: matched {len(version_records)} versions")
        else:
            print(f"  [PyPI fallback] No version matching {spec_str} on PyPI either")
            return None, None

    version_records.sort(key=lambda x: x[0], reverse=True)
    selected_version, latest_file = version_records[0]
    print(f"  [PyPI fallback] Selected: {latest_file} (version {selected_version})")

    file_url = urljoin(pypi_simple_url, latest_file)
    download_file(file_url, download_dir)
    return latest_file.split('/')[-1], str(selected_version)


def download_latest_version(base_url, package_name, download_dir, specifier=''):
    """
    下载给定包（package_name）的最新版本文件，可选地按版本约束过滤。

    Args:
        base_url (str): 私有仓库 simple index URL。
        package_name (str): 包名称。
        download_dir (str): 本地文件存储目录。
        specifier (str): 版本约束（如 "==2.1.0", ">=2.0" 或 ""）。

    版本选择语义：
      - 有 specifier：私有仓库中满足 specifier 的最高版本；若私有仓库无任何版本满足
        约束，返回 (None, None) —— 私有源对此约束不提供可用版本，不参与同名对比
        （pip 在该约束下不会安装私有源的该包，不应把违反约束的最高版误记为
        private version，否则会造成依赖混淆误报）。
      - 无 specifier：私有仓库的最高版本。
      - 私有源不可达 / 无文件：回退到 PyPI（download_from_pypi_simple）。

    Returns: (filename, version_str) — filename 为下载后的本地文件名（已剥离
    #sha256 fragment），version_str 为选中版本号；无可用版本时为 (None, None)。
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import parse as parse_ver

    spec_str = (specifier or '').strip()
    has_spec = bool(spec_str)

    package_url = urljoin(base_url.rstrip('/') + '/', index_package_slug(package_name) + '/')
    print(f"Visiting {package_url} (spec={spec_str or 'any'})...")

    session = _requests_session()
    try:
        response = session.get(package_url, timeout=INDEX_LOOKUP_TIMEOUT)
        response.raise_for_status()
    except Exception:
        print(f"  Failed to fetch {package_url}")
        print(f"  Private index unreachable, falling back to PyPI...")
        return download_from_pypi_simple(package_name, download_dir, spec_str)

    soup = BeautifulSoup(response.text, 'html.parser')

    files = []
    for link in soup.find_all('a', href=True):
        href = link['href']
        href_clean = href.split('#')[0]
        if href_clean.endswith(('.whl', '.tar.gz', '.zip')):
            files.append(href.rstrip('/'))

    if not files:
        print(f"No valid files found for package: {package_name}")
        print(f"  No files in private index, falling back to PyPI...")
        return download_from_pypi_simple(package_name, download_dir, spec_str)

    version_records = []
    for file_name in files:
        actual_file_name = file_name.rstrip('/').split('/')[-1]
        version_str = extract_version(actual_file_name, package_name)
        if not version_str:
            continue
        try:
            parsed = parse_ver(version_str)
            version_records.append((parsed, file_name))
        except InvalidVersion:
            continue

    if not version_records:
        print(f"No versioned files for {package_name}, taking first available")
        latest_file = files[0]
        latest_file_url = urljoin(package_url, latest_file)
        download_file(latest_file_url, download_dir)
        return latest_file.split(r'/')[-1], None

    # ── 版本过滤 ──
    if has_spec:
        spec_set = SpecifierSet(spec_str)
        filtered = [(v, fn) for v, fn in version_records if spec_set.contains(v)]
        if filtered:
            version_records = filtered
            print(f"  Spec {spec_str}: filtered to {len(version_records)} matching versions in private index")
        else:
            # 私有源有该包，但没有任何版本满足约束。
            # 此时 pip 不会安装私有源的该包（约束会排除其全部版本），
            # 因此既不能把"违反约束的私有最高版"当作 private version 记录（会导致
            # 依赖混淆误报：私有版 > 公有版 → 虚假 Critical），
            # 也不应把 PyPI 满足约束的版本误标为 private version（来源错配）。
            # 返回 (None, None)：私有源对此约束不提供可用版本，不参与同名对比。
            print(f"  No version matching {spec_str} in private index; "
                  f"private index provides no usable version for this requirement")
            return None, None

    # 选最高版本
    version_records.sort(key=lambda x: x[0], reverse=True)
    selected_version, latest_file = version_records[0]
    print(f"  Selected: {latest_file} (version {selected_version})")

    latest_file_url = urljoin(package_url, latest_file)
    download_file(latest_file_url, download_dir)
    return latest_file.split(r'/')[-1], str(selected_version)


META_DIR_SUFFIXES = '.dist-info', '.egg-info', 'EGG-INFO',
def get_attribute(metainfo, name, multiple=False):

    def attr_getter(_aname, default):
        _aname = _aname.replace('-', '_')
        return (
            getattr(metainfo, _aname, default)
            or getattr(metainfo, _aname.lower(), default)
        )

    def item_getter(_iname, getter, default):
        getter = getattr(metainfo, getter, None)
        if getter:
            return getter(_iname, default) or getter(_iname.lower(), default)
        return default

    if multiple:
        return (
            attr_getter(name, [])
            or item_getter(name, 'get_all', [])
            or item_getter(name, 'get', [])
            or []
        )
    else:
        return (
            attr_getter(name, None)
            or item_getter(name, 'get', None)
            or None
        )

def parse_metadata(location):
    # download_file 按 simple index href 原样存盘，文件名可能带 #sha256=... fragment。
    # 后缀派发必须用去掉 fragment 的名字（否则 .endswith('.whl') 等恒为 False），
    # 但真正打开磁盘文件要用原始 location（磁盘上确实以带 #sha256 的名字存盘）。
    location_no_frag = location.split('#')[0]

    print('-----------------------parse----------------',location)
    dependencies = []
    file_name=location_no_frag.split('/')[-1]
    version = extract_version(file_name)
    try:
        if location_no_frag.endswith(('.whl')):
            with zipfile.ZipFile(location) as zf:
                # print(location)
                for path in ZipPath(zf).iterdir():
                    if not path.name.endswith(META_DIR_SUFFIXES):
                        continue
                    for metapath in path.iterdir():
                        if not metapath.name.endswith('METADATA'):
                            continue

                        path=metapath
                        if not isinstance(metapath, (Path, ZipPath)):
                            path = Path(metapath)

                        parent = path.parent
                        if parent.name.endswith(META_DIR_SUFFIXES):
                            path = parent
                        dist = importlib_metadata.PathDistribution(path)
                        meta = dist.metadata
                        name = get_attribute(meta, 'Name')
                        version = get_attribute(meta, 'Version')
                        dependencies = dist.requires
                        author = get_attribute(meta, 'Author')
                        author_email = get_attribute(meta, 'Author-email')
                        return dependencies, version

        elif location_no_frag.endswith(".tar.gz"):
            with tarfile.open(location, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile() and member.name.endswith('PKG-INFO'):
                        # Extract the file's content
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            pkg_info_content = file_obj.read().decode('utf-8')
                            metadata = Parser().parsestr(pkg_info_content)
                            licenses = metadata.get('License')
                            if not licenses:  # 检查许可证是否为空
                                classifiers = metadata.get_all('Classifier')
                                if classifiers:
                                    for classifier in classifiers:
                                        if classifier.startswith("License ::"):
                                            licenses = classifier.replace("License :: ", "").strip()
                                            break
                            version = metadata.get('Version')
                            dependencies = metadata.get_all('Requires-Dist') or []
                            return dependencies, version

        elif location_no_frag.endswith(".zip"):
            # Open the ZIP file
            with zipfile.ZipFile(location, 'r') as zipf:
                for member in zipf.namelist():
                    if member.endswith('PKG-INFO'):
                        with zipf.open(member) as file_obj:
                            pkg_info_content = file_obj.read().decode('utf-8')
                            metadata = Parser().parsestr(pkg_info_content)
                            version = metadata.get('Version')
                            scopes = metadata.get_all('Provides-Extra') or []
                            dependencies = metadata.get_all('Requires-Dist') or []
                            return dependencies, version

    except Exception as e:
        print(f"Error parsing {location}: {str(e)}")
        return [], version

    return [], version


def evaluate_environment_marker(marker_str, requested_extras=None):
    """
    评估环境标记是否符合当前环境。
    参考 pip 的 behavior：只安装符合当前环境的依赖。

    Args:
        marker_str: 环境标记字符串（如 'platform_system == "Linux" and extra == "all"'）
        requested_extras: 当前包被请求时指定的 extras 集合（如 {'cublas', 'cudart', ...}）
                          当为 None 时，表示该包未被请求 extras，extra == 标记评估为 False。
                          当为非空集合时，extra == "X" 评估为 X in requested_extras。
                          当 extra == "all" 且 requested_extras 非空时，评估为 True。
    """
    if not marker_str:
        return True
    
    marker_str = marker_str.strip()
    
    # 如果有 extras 被请求，使用 packaging.markers 正确评估
    if requested_extras is not None:
        try:
            from packaging.markers import Marker
            marker = Marker(marker_str)
            # 为每个请求的 extra 评估标记
            for extra_name in requested_extras:
                if marker.evaluate({'extra': extra_name}):
                    return True
            # 检查 extra == "all" 的情况
            if marker.evaluate({'extra': 'all'}):
                return True
            return False
        except Exception:
            pass
    else:
        # 无 extras 请求时，先尝试用 packaging.markers 评估（处理非 extra 标记）
        try:
            from packaging.markers import Marker
            marker = Marker(marker_str)
            # 不传 extra 参数，让不涉及 extra 的标记正常评估
            # 涉及 extra 的标记在无 extras 请求时返回 False
            if 'extra' not in marker_str.lower():
                return marker.evaluate()
        except Exception:
            pass
    
    import sys
    import platform
    
    marker_lower = marker_str.lower()
    
    if 'extra ==' in marker_str or 'extra==' in marker_str:
        return False
    
    if 'os_name' in marker_lower:
        os_name = sys.platform.lower()
        if 'posix' in os_name:
            current_os = 'posix'
        elif 'nt' in os_name:
            current_os = 'nt'
        else:
            current_os = 'posix'
        
        if f"os_name == '{current_os}'" not in marker_str and \
           f'os_name=="{current_os}"' not in marker_str:
            return False
    
    if 'sys_platform' in marker_lower:
        sys_platform = sys.platform
        if f"sys_platform == '{sys_platform}'" not in marker_str and \
           f'sys_platform=="{sys_platform}"' not in marker_str:
            return False
    
    if 'platform_system' in marker_lower:
        platform_system = platform.system()
        if f"platform_system == '{platform_system}'" not in marker_str and \
           f'platform_system=="{platform_system}"' not in marker_str:
            return False
    
    return True


def parse_requires_dist(requirement_line, current_package=None):
    """
    解析 wheel metadata 中的 Requires-Dist 行，保留版本约束。
    新增：过滤条件依赖和不符合当前环境的依赖，与 pip 行为对齐。

    Args:
        requirement_line: Requires-Dist 行（如 'nvidia-cublas; extra == "all"'）
        current_package: 当前正在展开的包名（如 'cuda-toolkit'），用于查找其请求的 extras
    """
    line = (requirement_line or '').strip()
    if not line:
        return None

    parts = line.split(';', 1)
    req_part = parts[0].strip()
    marker_str = parts[1].strip() if len(parts) > 1 else ''
    
    # 获取当前包被请求的 extras（如有）
    requested_extras = None
    if current_package:
        pkg_canon = canonicalize_name(current_package)
        if pkg_canon in _package_requested_extras:
            requested_extras = _package_requested_extras[pkg_canon]
    
    if not evaluate_environment_marker(marker_str, requested_extras=requested_extras):
        return None

    try:
        req = Requirement(req_part)
    except Exception:
        return None
    if not req.name:
        return None

    # 如果该依赖自身指定了 extras（如 cuda-toolkit[cublas,...]），记录下来供后续展开使用
    if req.extras:
        child_canon = canonicalize_name(req.name)
        existing = _package_requested_extras.get(child_canon, set())
        _package_requested_extras[child_canon] = existing | set(req.extras)

    return {
        'name': canonicalize_name(req.name),
        'specifier': str(req.specifier),
        'raw_requirement': line,
    }


def parent_already_listed(parents, new_parent):
    for parent in parents:
        if isinstance(parent, dict) and isinstance(new_parent, dict):
            if (
                parent.get('name') == new_parent.get('name')
                and parent.get('source') == new_parent.get('source')
            ):
                return True
        elif parent == new_parent:
            return True
    return False


def record_indirect_constraint(
    indirect_specifiers,
    indirect_declaration_rows,
    child_dep,
    parent_name,
    parent_source='private',
):
    child_name = child_dep['name']
    indirect_specifiers[child_name] = merge_specifiers(
        indirect_specifiers.get(child_name, ''),
        child_dep.get('specifier', ''),
    )

    if child_name not in indirect_declaration_rows:
        indirect_declaration_rows[child_name] = {
            'name': child_name,
            'specifier': indirect_specifiers[child_name],
            'raw_requirement': child_dep.get('raw_requirement', child_name),
            'constraint_source': 'indirect',
            'from_parents': [{'name': parent_name, 'source': parent_source}],
        }
        return

    row = indirect_declaration_rows[child_name]
    row['specifier'] = indirect_specifiers[child_name]
    row['raw_requirement'] = (
        f"{row['raw_requirement']} ; {child_dep.get('raw_requirement', child_name)}"
    )
    parent_entry = {'name': parent_name, 'source': parent_source}
    if not parent_already_listed(row['from_parents'], parent_entry):
        row['from_parents'].append(parent_entry)


def _enqueue_dependency_edges(
    dependency_lines,
    parent_name,
    parent_source,
    indirect_specifiers,
    indirect_declaration_rows,
    queue,
    enqueued,
):
    child_names = []
    for dep_line in dependency_lines or []:
        child_dep = parse_requires_dist(dep_line, current_package=parent_name)
        if not child_dep:
            continue
        record_indirect_constraint(
            indirect_specifiers,
            indirect_declaration_rows,
            child_dep,
            parent_name=parent_name,
            parent_source=parent_source,
        )
        child_name = child_dep['name']
        child_specifier = child_dep.get('specifier', '')
        child_names.append((child_name, child_specifier))
        if child_name not in enqueued:
            queue.put(child_name)
            enqueued.add(child_name)
    return child_names


def get_pypi_version_requires_dist(package_name, specifier_str=''):
    """
    获取指定版本约束的包的 requires_dist。
    如果没有版本约束，获取最新版本。
    """
    cache_key = f"{package_name}:{specifier_str}"
    if cache_key in _pypi_requires_dist_cache:
        return _pypi_requires_dist_cache[cache_key]

    package_url = f"{PYPI_JSON_BASE}/{package_name}/json"

    for attempt in range(3):
        try:
            response = requests.get(package_url, timeout=30, verify=True)
            if response.status_code == 200:
                data = response.json()
                info = data.get('info', {}) or {}
                
                if specifier_str:
                    try:
                        specifier = SpecifierSet(specifier_str)
                        versions = data.get('releases', {}).keys()
                        valid_versions = []
                        for v in versions:
                            try:
                                if parse_version(v) in specifier:
                                    valid_versions.append(parse_version(v))
                            except InvalidVersion:
                                continue
                        
                        if valid_versions:
                            selected_version = max(valid_versions)
                            version_url = f"{PYPI_JSON_BASE}/{package_name}/{selected_version}/json"
                            version_response = requests.get(version_url, timeout=30, verify=True)
                            if version_response.status_code == 200:
                                version_info = version_response.json().get('info', {})
                                requires = version_info.get('requires_dist') or []
                                _pypi_requires_dist_cache[cache_key] = requires
                                return requires
                    except Exception:
                        pass
                
                requires = info.get('requires_dist') or []
                _pypi_requires_dist_cache[cache_key] = requires
                return requires
            elif attempt < 2:
                import time
                time.sleep(2 ** attempt)
                continue
        except requests.RequestException:
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
                continue
    
    _pypi_requires_dist_cache[cache_key] = []
    return []


def get_pypi_requires_dist(package_name):
    return get_pypi_version_requires_dist(package_name, '')


# 分析项目依赖：mixed-source expanded BFS（默认）或 legacy 公有剪枝模式
def analyze_project_dependencies(
    project_package,
    private_urls,
    prune_public_branches=False,
    direct_specifiers=None,
):
    """
    Mixed-source BFS:
      - 私有 index 上的包：从私有源下载 metadata 并记录私有版本
      - 仅公有的包：从 PyPI requires_dist 继续扩展（不剪枝）
      - legacy 模式 prune_public_branches=True 时，在公有节点停止扩展。
    direct_specifiers: {pkg_name: specifier_str} 版本约束
    """
    if isinstance(private_urls, str):
        private_urls = [private_urls]
    direct_specifiers = direct_specifiers or {}

    project_private_package = {}
    indirect_specifiers = {}
    indirect_declaration_rows = {}
    bfs_edges = []  # 记录每条 BFS 展开边
    package_details = {}  # 记录每个包在 BFS 中的完整信息
    # 重置 extras 跟踪，避免跨项目数据污染
    _package_requested_extras.clear()
    traversal_stats = {
        'traversal_mode': 'pruned_public_branches' if prune_public_branches else 'mixed_source_expanded',
        'private_index_expansions': 0,
        'pypi_metadata_expansions': 0,
        'pruned_public_nodes': 0,
    }
    private_url_cache = {}

    def get_private_url(pkg_name):
        if pkg_name not in private_url_cache:
            private_url_cache[pkg_name] = resolve_private_index_url(pkg_name, private_urls)
        return private_url_cache[pkg_name]

    os.makedirs(download_dir, exist_ok=True)
    queue = Queue()
    enqueued = set()
    expanded_private = set()
    expanded_public = set()

    for pkg_name in project_package:
        if pkg_name not in enqueued:
            queue.put(pkg_name)
            enqueued.add(pkg_name)

    while not queue.empty():
        pkg_name = queue.get()
        private_url = get_private_url(pkg_name)

        if private_url:
            if pkg_name in expanded_private:
                continue
            expanded_private.add(pkg_name)
            # 获取合并后的版本约束
            effective_spec = indirect_specifiers.get(pkg_name, '')
            if pkg_name in direct_specifiers:
                ds = direct_specifiers[pkg_name] or ''
                if effective_spec and ds:
                    effective_spec = merge_specifiers(effective_spec, ds)
                elif ds:
                    effective_spec = ds

            dependency_lines = []
            downloaded_version = None
            try:
                pkg_whl_file, download_version = download_latest_version(private_url, pkg_name, download_dir, specifier=effective_spec)
                if not pkg_whl_file:
                    raise RuntimeError(f'no wheel for {pkg_name}')
                location = os.path.join(download_dir, pkg_whl_file)
                dependency_lines, version = parse_metadata(location)
                if version is None:
                    version = download_version
                project_private_package[pkg_name] = version
                downloaded_version = version
                traversal_stats['private_index_expansions'] += 1
            except Exception as e:
                dependency_lines = []
                downloaded_version = None

            child_names = _enqueue_dependency_edges(
                dependency_lines,
                parent_name=pkg_name,
                parent_source='private',
                indirect_specifiers=indirect_specifiers,
                indirect_declaration_rows=indirect_declaration_rows,
                queue=queue,
                enqueued=enqueued,
            )
            # 记录私有包详情
            package_details[pkg_name] = {
                'version': downloaded_version or '',
                'source': 'private',
                'private_url': private_url,
                'specifier': effective_spec,
                'requires_dist': [{'name': cn, 'specifier': cs} for cn, cs in child_names],
            }
            for child_name, child_spec in child_names:
                bfs_edges.append({
                    'parent': pkg_name,
                    'parent_source': 'private',
                    'parent_version': downloaded_version or '',
                    'child': child_name,
                })
            if child_names:
                print(f'private expand {pkg_name} ->', [c[0] for c in child_names])
            continue

        if prune_public_branches:
            traversal_stats['pruned_public_nodes'] += 1
            package_details[pkg_name] = {
                'version': '',
                'source': 'public (pruned)',
                'private_url': '',
                'specifier': '',
                'requires_dist': [],
            }
            continue

        if pkg_name in expanded_public:
            continue
        expanded_public.add(pkg_name)

        effective_spec = indirect_specifiers.get(pkg_name, '')
        if pkg_name in direct_specifiers:
            ds = direct_specifiers[pkg_name] or ''
            if effective_spec and ds:
                effective_spec = merge_specifiers(effective_spec, ds)
            elif ds:
                effective_spec = ds

        dependency_lines = get_pypi_version_requires_dist(pkg_name, effective_spec)
        traversal_stats['pypi_metadata_expansions'] += 1
        child_names = _enqueue_dependency_edges(
            dependency_lines,
            parent_name=pkg_name,
            parent_source='public',
            indirect_specifiers=indirect_specifiers,
            indirect_declaration_rows=indirect_declaration_rows,
            queue=queue,
            enqueued=enqueued,
        )
        # 记录公有包详情
        package_details[pkg_name] = {
            'version': '',
            'source': 'public',
            'private_url': '',
            'specifier': '',
            'requires_dist': [{'name': cn, 'specifier': cs} for cn, cs in child_names],
        }
        for child_name, child_spec in child_names:
            bfs_edges.append({
                'parent': pkg_name,
                'parent_source': 'public',
                'parent_version': '',
                'child': child_name,
            })
        if child_names:
            print(f'pypi expand {pkg_name} ->', [c[0] for c in child_names])

    return (project_private_package, indirect_specifiers,
            indirect_declaration_rows, traversal_stats, bfs_edges, package_details)


def derive_pruned_results(bfs_edges, project_private_package_expanded,
                           direct_dependency_package,
                           indirect_specifiers_expanded,
                           indirect_declaration_rows_expanded,
                           package_details_expanded):
    """
    从 expanded-mode BFS 结果中推导 pruned 子集。
    只有"祖先链全为 private-index 包"的节点才保留。
    """
    from collections import defaultdict

    # 构建 parent → [(child, parent_source)] 邻接表
    children_of = defaultdict(list)
    for edge in bfs_edges:
        children_of[edge['parent']].append((edge['child'], edge['parent_source']))

    # BFS 前向传播 pruned_eligible：根节点（直接依赖）默认 eligible
    pruned_eligible = set(direct_dependency_package)
    queue = list(direct_dependency_package)
    while queue:
        parent = queue.pop(0)
        for child, parent_source in children_of.get(parent, []):
            # 只有 private parent 才能把 eligible 传给 child
            if parent_source == 'private' and child not in pruned_eligible:
                pruned_eligible.add(child)
                queue.append(child)

    # 过滤各数据结构
    ppp = {k: v for k, v in project_private_package_expanded.items() if k in pruned_eligible}
    iss = {k: v for k, v in indirect_specifiers_expanded.items() if k in pruned_eligible}
    idr = {k: v for k, v in indirect_declaration_rows_expanded.items() if k in pruned_eligible}
    pd = {k: v for k, v in package_details_expanded.items() if k in pruned_eligible}
    be = [e for e in bfs_edges if e['parent'] in pruned_eligible]

    # 统计
    private_count = len(ppp)
    pruned_count = sum(1 for k, v in package_details_expanded.items()
                       if k not in pruned_eligible and v.get('source') == 'public')

    traversal_stats_pruned = {
        'traversal_mode': 'pruned_public_branches',
        'private_index_expansions': private_count,
        'pypi_metadata_expansions': 0,
        'pruned_public_nodes': pruned_count,
    }

    return ppp, iss, idr, traversal_stats_pruned, be, pd


_pypi_versions_cache = {}


def get_pypi_all_versions(package_name):
    if package_name in _pypi_versions_cache:
        return _pypi_versions_cache[package_name]

    package_url = f"{PYPI_JSON_BASE}/{package_name}/json"

    import time
    for attempt in range(3):
        try:
            response = requests.get(package_url, timeout=30)
            if response.status_code == 200:
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
        except requests.exceptions.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            _pypi_versions_cache[package_name] = []
            return []
    else:
        _pypi_versions_cache[package_name] = []
        return []

    if response.status_code != 200:
        _pypi_versions_cache[package_name] = []
        return []

    releases = response.json().get('releases', {})
    valid_versions = []
    for version in releases.keys():
        try:
            parse(version)
            valid_versions.append(version)
        except InvalidVersion:
            continue

    _pypi_versions_cache[package_name] = valid_versions
    return valid_versions


def filter_versions_by_specifier(versions, specifier_str):
    specifier_str = (specifier_str or '').strip()
    if not specifier_str:
        return list(versions)

    spec = SpecifierSet(specifier_str)
    satisfying = []
    for version in versions:
        try:
            parsed = parse(version)
        except InvalidVersion:
            continue
        if spec.contains(parsed):
            satisfying.append(version)
    return satisfying


def get_public_version_for_comparison(package_name, declared_specifier=None):
    """
    约束感知公开版本选择：
    - 有版本约束（直接声明或间接 Requires-Dist）时，只在满足 specifier 的 PyPI 版本里取最高版本；
    - 无约束时，使用 PyPI 全局最高版本。
    返回: (comparison_public_version, highest_unconstrained, satisfying_versions)
    """
    all_versions = get_pypi_all_versions(package_name)
    if not all_versions:
        return None, None, []

    highest_unconstrained = max(all_versions, key=parse)
    declared_specifier = (declared_specifier or '').strip()
    if not declared_specifier:
        return highest_unconstrained, highest_unconstrained, all_versions

    satisfying_versions = filter_versions_by_specifier(all_versions, declared_specifier)
    if not satisfying_versions:
        return None, highest_unconstrained, []

    highest_satisfying = max(satisfying_versions, key=parse)
    return highest_satisfying, highest_unconstrained, satisfying_versions


def _is_specifier_effectively_pinned(spec_str):
    """
    判断版本约束是否为精确锁定（==X）。

    精确锁定的包即使 PyPI 存在同版本，pip 也会从主索引获取，
    不构成真正的依赖混淆。只有范围约束（>=X 或无约束）时，
    PyPI 的高版本才能覆盖私有源。
    """
    spec_str = (spec_str or '').strip()
    if not spec_str:
        return False
    for constraint in spec_str.split(','):
        constraint = constraint.strip()
        if constraint.startswith('==') or constraint.startswith('==='):
            return True
    return False


def get_effective_specifier(package_name, direct_specifiers, indirect_specifiers):
    direct_spec = (direct_specifiers.get(package_name) or '').strip()
    if direct_spec:
        return direct_spec, 'direct'
    indirect_spec = (indirect_specifiers.get(package_name) or '').strip()
    if indirect_spec:
        return indirect_spec, 'indirect'
    return '', ''


def build_same_name_entry(
    package_name,
    private_version,
    declared_specifier=None,
    constraint_source='',
):
    public_version, highest_unconstrained, satisfying_versions = get_public_version_for_comparison(
        package_name,
        declared_specifier,
    )
    entry = {
        "private version": str(private_version),
        "public version": str(public_version) if public_version is not None else None,
        "highest public (unconstrained)": str(highest_unconstrained) if highest_unconstrained is not None else None,
    }
    if declared_specifier:
        entry["declared specifier"] = declared_specifier
        entry["constraint source"] = constraint_source or "unknown"
        entry["satisfying public versions count"] = len(satisfying_versions)
    return entry, public_version


def get_pypi_package_info(package_name):
    all_versions = get_pypi_all_versions(package_name)
    if not all_versions:
        return None
    return max(all_versions, key=parse)




def judge_dependency_confusion(project_directory, output_path, prune_public_branches=False):
    dependency_confusion_result_info = {}
    types = [
        'direct dependency',
        'indirect private dependency',
        'direct dependency with same name',
        'indirect private dependency with same name'
    ]
    for t in types:
        dependency_confusion_result_info[t] = {'number': 0, 'packages': {}}
    dependency_confusion_result_info['risk'] = ''

    # 1. 扫描私有仓库 URL
    private_urls, file_private_sources = scan_private_urls(project_directory)
    # 写出 private_sources.txt
    _write_private_sources(output_path, project_directory, file_private_sources)
    print(f"Private URLs: {private_urls}")
    if not private_urls:
        print('No dependency confusion !!!')
        return

    # 2. 获取python项目所有直接依赖（含版本约束）
    direct_dependency_declarations = get_dependency(project_directory, output_path)
    direct_dependency_package = [dep['name'] for dep in direct_dependency_declarations]
    direct_dependency_specifiers = {
        dep['name']: dep.get('specifier', '')
        for dep in direct_dependency_declarations
    }
    # 实际全部的直接依赖包
    # direct_dependency_package=[]
    # with open(os.path.join(output_path,"direct_dependency_packages_truth.txt"), encoding="utf-8") as f:
    #     direct_dependency_package = [line.strip() for line in f if line.strip()]


    # 3. 获取私有仓库中所有包，以及间接依赖的版本约束
    project_private_packages = {}
    indirect_dependency_specifiers = {}
    indirect_dependency_declarations = {}
    traversal_stats_merged = {
        'traversal_mode': 'pruned_public_branches' if prune_public_branches else 'mixed_source_expanded',
        'private_index_expansions': 0,
        'pypi_metadata_expansions': 0,
        'pruned_public_nodes': 0,
    }
    sorted_private_urls = sorted(private_urls)
    print(
        f'Private indexes: {len(sorted_private_urls)} '
        f'(on-demand per-package lookup; full listing not required)'
    )
    project_private_package, indirect_specs, indirect_rows, traversal_stats, bfs_edges, package_details = analyze_project_dependencies(
        direct_dependency_package,
        sorted_private_urls,
        prune_public_branches=prune_public_branches,
        direct_specifiers=direct_dependency_specifiers,
    )
    project_private_packages.update(project_private_package)
    for key in traversal_stats_merged:
        if key != 'traversal_mode':
            traversal_stats_merged[key] += traversal_stats.get(key, 0)
    for name, spec in indirect_specs.items():
        indirect_dependency_specifiers[name] = merge_specifiers(
            indirect_dependency_specifiers.get(name, ''),
            spec,
        )
    for name, row in indirect_rows.items():
        if name in indirect_dependency_declarations:
            existing = indirect_dependency_declarations[name]
            existing['specifier'] = indirect_dependency_specifiers[name]
            existing['raw_requirement'] = (
                f"{existing['raw_requirement']} ; {row['raw_requirement']}"
            )
            for parent in row.get('from_parents', []):
                if not parent_already_listed(existing['from_parents'], parent):
                    existing['from_parents'].append(parent)
        else:
            indirect_dependency_declarations[name] = row
    for name in indirect_dependency_specifiers:
        if name in indirect_dependency_declarations:
            indirect_dependency_declarations[name]['specifier'] = indirect_dependency_specifiers[name]

    # 4. 写直接依赖列表（含声明约束）
    dependency_confusion_result_info[types[0]]['number'] = len(direct_dependency_declarations)
    dependency_confusion_result_info[types[0]]['packages'] = direct_dependency_declarations
    with open(os.path.join(output_path, 'direct_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in direct_dependency_declarations:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")

    # 6. 写间接依赖列表（含 Requires-Dist 传播的版本约束）
    indirect_dependency_package = sorted(
        set(indirect_rows.keys()) - set(direct_dependency_package)
    )
    indirect_dependency_declaration_list = [
        indirect_dependency_declarations[name]
        for name in indirect_dependency_package
        if name in indirect_dependency_declarations
    ]
    for name in indirect_dependency_package:
        if name not in indirect_dependency_declarations:
            indirect_dependency_declaration_list.append({
                'name': name,
                'specifier': '',
                'raw_requirement': name,
                'constraint_source': 'indirect',
                'from_parents': [],
            })

    dependency_confusion_result_info['traversal'] = traversal_stats_merged
    with open(os.path.join(output_path, 'traversal_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(traversal_stats_merged, f, indent=4)

    # 保存 BFS 展开边 (中间结果)
    with open(os.path.join(output_path, 'bfs_edges.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(bfs_edges), f, indent=2)

    # 保存每个包的完整信息：版本、来源(public/private)、依赖列表
    with open(os.path.join(output_path, 'package_details.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(package_details), f, indent=2)

    # 保存按配置文件的直接依赖分组
    direct_by_file = {}
    for dep in direct_dependency_declarations:
        sf = dep.get('source_file', '')
        if sf not in direct_by_file:
            direct_by_file[sf] = []
        direct_by_file[sf].append({
            'name': dep['name'],
            'specifier': dep.get('specifier', ''),
        })
    with open(os.path.join(output_path, 'direct_by_config_file.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(direct_by_file), f, indent=2)

    dependency_confusion_result_info[types[1]]['number'] = len(indirect_dependency_package)
    dependency_confusion_result_info[types[1]]['packages'] = indirect_dependency_declaration_list
    with open(os.path.join(output_path, 'indirect_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in indirect_dependency_declaration_list:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")
            for parent in dep.get('from_parents', []):
                parent_name = parent.get('name', parent) if isinstance(parent, dict) else parent
                parent_source = parent.get('source', '') if isinstance(parent, dict) else ''
                suffix = f' ({parent_source})' if parent_source else ''
                f.write(f"  # from parent package: {parent_name}{suffix}\n")
    with open(os.path.join(output_path, 'direct_and_indirect_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in direct_dependency_declarations:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")
        for dep in indirect_dependency_declaration_list:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")

    # 7. 构造同名包映射
    # 7-1 所有同名包（用于 same_name_dependencies.json）
    all_same_name_packages = {}
    # 7-2 仅 public > private（用于 dependency_confusion.txt）
    package_public_private_version = {}

    for package, private_version in project_private_packages.items():
        declared_specifier, constraint_source = get_effective_specifier(
            package,
            direct_dependency_specifiers,
            indirect_dependency_specifiers,
        )
        entry, public_version = build_same_name_entry(
            package,
            private_version,
            declared_specifier=declared_specifier,
            constraint_source=constraint_source,
        )
        if public_version is None:
            continue

        all_same_name_packages[package] = entry
        if parse(public_version) >= parse(private_version):
            package_public_private_version[package] = entry

    # 8. 同名包按类型分类
    json_direct = {}
    json_indirect = {}
    direct_public_gt_private = {}
    indirect_public_gt_private = {}

    for pkg in direct_dependency_package:
        if pkg in all_same_name_packages:
            json_direct[pkg] = all_same_name_packages[pkg]
        if pkg in package_public_private_version:
            direct_public_gt_private[pkg] = package_public_private_version[pkg]

    for pkg in indirect_dependency_package:
        if pkg in all_same_name_packages:
            json_indirect[pkg] = all_same_name_packages[pkg]
        if pkg in package_public_private_version:
            indirect_public_gt_private[pkg] = package_public_private_version[pkg]

    # 9. 写入 dependency_confusion.txt（仅 public > private）
    dependency_confusion_result_info[types[2]] = {
        'number': len(direct_public_gt_private),
        'packages': direct_public_gt_private
    }
    dependency_confusion_result_info[types[3]] = {
        'number': len(indirect_public_gt_private),
        'packages': indirect_public_gt_private
    }

    if package_public_private_version:
        dependency_confusion_result_info['risk'] = (
            'Critical: Public library has same-name and higher version packages'
        )
    else:
        dependency_confusion_result_info['risk'] = 'No confusion'

    with open(os.path.join(output_path, 'dependency_confusion.txt'), 'w') as file:
        json.dump(convert_sets_to_lists(dependency_confusion_result_info), file, indent=5)

    # 10. 写入 same_name_dependencies.json（所有同名包）
    same_name_output = {
        "direct dependency with same name": {
            "number": len(json_direct),
            "packages": json_direct
        },
        "indirect dependency with same name": {
            "number": len(json_indirect),
            "packages": json_indirect
        }
    }
    with open(os.path.join(output_path, 'same_name_dependencies.txt'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(same_name_output), f, indent=4)

# 示例：
# url = "https://antocuni.github.io/pypy-wheels/manylinux2010/"

# def judge_dependency_confusion(project_directory,output_path):
#     dependency_confusion_result_info={}
#     type=['direct dependency','indirect private dependency','direct dependency with same name','indirect private dependency with same name']
#     for t in type:
#         dependency_confusion_result_info[t]={}
#         dependency_confusion_result_info[t]['number']=0
#         dependency_confusion_result_info[t]['packages']=[]
#     dependency_confusion_result_info['risk']=''
#
#     # 1. 分析python项目 --index-url以及--extra-index-url  分析是否存在内部包的url
#     private_urls = scan_private_urls(project_directory)
#     print(private_urls)
#     if (len(private_urls) == 0):
#         print('No dependency confusion !!!')
#         # dependency_confusion_result_info['risk'] = 'No confusion'
#         return
#     # 2. 获取python项目所有依赖包 direct_dependency_package 直接依赖包
#     # direct_dependency_package = get_dependency(project_directory)
#     # 实际全部的直接依赖包
#     direct_dependency_package=[]
#     with open(os.path.join(output_path,"direct_dependency_packages_truth.txt"), encoding="utf-8") as f:
#         direct_dependency_package = [line.strip() for line in f if line.strip()]
#
#
#     # 3. 获取内部包url上所有的包 url_package
#     project_private_packages={}
#     for url in private_urls:
#         url_package=fetch_package_names_from_url(url)
#         project_private_package=analyze_project_dependencies(direct_dependency_package, url_package,url)
#         project_private_packages.update(project_private_package)
#         # print(project_private_package.items())
#
#     private_related_public_package={}
#     for package_name, version in project_private_packages.items():
#         highest_version=get_pypi_package_info(package_name)
#         private_related_public_package[package_name]=highest_version
#
#     print('direct dependency packages:',len(direct_dependency_package))
#     print(direct_dependency_package)
#     dependency_confusion_result_info[type[0]]['number']=len(direct_dependency_package)
#     dependency_confusion_result_info[type[0]]['packages']=direct_dependency_package
#     f=open(os.path.join(output_path,'direct_dependency_packages.txt'),'w')
#     for d in direct_dependency_package:
#         f.write('{}\n'.format(d))
#     f.close()
#
#
#     indirect_dependency_package=set(project_private_packages.keys()).difference(set(direct_dependency_package))
#     print('indirect dependency packages:',len(indirect_dependency_package))
#     print(indirect_dependency_package)
#     dependency_confusion_result_info[type[1]]['number']=len(indirect_dependency_package)
#     dependency_confusion_result_info[type[1]]['packages']=indirect_dependency_package
#     f=open(os.path.join(output_path,'direct_and_indirect_dependency_packages.txt'),'w')
#     for d in set(set(project_private_packages.keys()).union(set(direct_dependency_package))):
#         f.write('{}\n'.format(d))
#     f.close()
#
#
#
#
#     if(len(project_private_packages)==0):
#         print('No confusion')
#         dependency_confusion_result_info['risk']='No confusion'
#         dependency_confusion_result_info[type[2]]['number'] = 0
#         dependency_confusion_result_info[type[2]]['packages'] = []
#         dependency_confusion_result_info[type[3]]['number'] = 0
#         dependency_confusion_result_info[type[3]]['packages'] = []
#     else:
#         package_public_private_version={}
#         version_flags=[]#1 denote private version higher, 0 denote public version higher, -1 denote no public
#         flag=0
#         for package,private_version in project_private_packages.items():
#             public_version=private_related_public_package[package]
#             if(public_version==None):
#                 flag=-1
#                 version_flags.append(flag)
#                 continue
#             elif(public_version>private_version):
#                 flag=0
#             else:
#                 flag=1
#             package_public_private_version[package]={}
#             package_public_private_version[package]={'public version':public_version,'private version':private_version}
#             version_flags.append(flag)
#
#         direct_dependency_common_count=0
#         indirect_dependency_common_count=0
#         direct_dependency_with_same_name=[]
#         indirect_dependency_with_same_name=[]
#         direct_dependency_version_with_same_name={}
#         indirect_dependency_version_with_same_name={}
#         for direct_depedency in direct_dependency_package:
#             if(direct_depedency in private_related_public_package):
#                 direct_dependency_common_count+=1
#                 # dependency_confusion_result_info[type[2]]['ppackage_public_private_version[package]
#                 direct_dependency_with_same_name.append(direct_depedency)
#                 if(direct_depedency in package_public_private_version):
#                     direct_dependency_version_with_same_name[direct_depedency]=package_public_private_version[direct_depedency]
#                 else:
#                     direct_dependency_version_with_same_name[direct_depedency] =[]
#         for indirect_dependency in indirect_dependency_package:
#             if(indirect_dependency in private_related_public_package):
#                 indirect_dependency_common_count+=1
#                 indirect_dependency_with_same_name.append(indirect_dependency)
#                 indirect_dependency_version_with_same_name[indirect_dependency] = package_public_private_version[indirect_dependency]
#
#         print('Direct dependencies with the same name:',direct_dependency_common_count)
#         print('Indirect dependencies with the same name',indirect_dependency_common_count)
#
#         dependency_confusion_result_info[type[2]]['number'] = direct_dependency_common_count
#         dependency_confusion_result_info[type[2]]['packages'] = direct_dependency_version_with_same_name
#         dependency_confusion_result_info[type[3]]['number'] = indirect_dependency_common_count
#         dependency_confusion_result_info[type[3]]['packages'] = indirect_dependency_version_with_same_name
#
#
#         if(0 in version_flags):
#             print('Critical: Public library has same-name and higher version packages')
#             dependency_confusion_result_info['risk']='Critical: Public library has same-name and higher version packages'
#         elif(-1 in version_flags):
#             print('Low: There is no same-name package in the public library.')
#             dependency_confusion_result_info['risk']='Low: There is no same-name package in the public library.'
#         else:
#             print('High: There are same-name packages in the public library, all with versions lower than those in the private library, but there are potential conflicts.')
#             dependency_confusion_result_info['risk']='High: There are same-name packages in the public library, all with versions lower than those in the private library, but there are potential conflicts.'
#     with open(os.path.join(output_path,'dependency_confusion.txt'),'w') as file:
#         json.dump(convert_sets_to_lists(dependency_confusion_result_info),file, indent=5)
#
#     # ✅ 合并输出同名依赖到一个文件
#     same_name_output = {
#         "direct dependency with same name": {
#             "number": len(direct_dependency_version_with_same_name),
#             "packages": direct_dependency_version_with_same_name
#         },
#         "indirect dependency with same name": {
#             "number": len(indirect_dependency_version_with_same_name),
#             "packages": indirect_dependency_version_with_same_name
#         }
#     }
#
#     with open(os.path.join(output_path, 'same_name_dependencies.txt'), 'w', encoding='utf-8') as f:
#         json.dump(convert_sets_to_lists(same_name_output), f, indent=4)

# def scan_all_dirs(root_path):
#     dirs = []
#     for dirpath, dirnames, filenames in os.walk(root_path):
#         for dirname in dirnames:
#             full_dir_path = os.path.join(dirpath, dirname)
#             dirs.append(full_dir_path)
#     return dirs

TRAVERSE_MODE_EXPANDED = 'expanded'
TRAVERSE_MODE_PRUNED = 'pruned'


def _write_bfs_outputs(output_path, project_directory,
                        project_private_packages, direct_dependency_declarations,
                        direct_dependency_package, direct_dependency_specifiers,
                        indirect_dependency_specifiers, indirect_dependency_declarations,
                        traversal_stats, bfs_edges, package_details, file_private_sources):
    """将 BFS 分析结果写出到 output_path 目录。"""
    types = [
        'direct dependency',
        'indirect private dependency',
        'direct dependency with same name',
        'indirect private dependency with same name'
    ]

    os.makedirs(output_path, exist_ok=True)
    _write_private_sources(output_path, project_directory, file_private_sources)

    # 写直接依赖列表
    with open(os.path.join(output_path, 'direct_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in direct_dependency_declarations:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")

    # 间接依赖列表
    indirect_dependency_package = sorted(
        set(indirect_dependency_declarations.keys()) - set(direct_dependency_package)
    )
    indirect_dependency_declaration_list = [
        indirect_dependency_declarations[name]
        for name in indirect_dependency_package
        if name in indirect_dependency_declarations
    ]
    for name in indirect_dependency_package:
        if name not in indirect_dependency_declarations:
            indirect_dependency_declaration_list.append({
                'name': name, 'specifier': '', 'raw_requirement': name,
                'constraint_source': 'indirect', 'from_parents': [],
            })

    with open(os.path.join(output_path, 'traversal_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(traversal_stats, f, indent=4)

    with open(os.path.join(output_path, 'bfs_edges.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(bfs_edges), f, indent=2)

    with open(os.path.join(output_path, 'package_details.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(package_details), f, indent=2)

    # 按配置文件的直接依赖分组
    direct_by_file = {}
    for dep in direct_dependency_declarations:
        sf = dep.get('source_file', '')
        direct_by_file.setdefault(sf, []).append({
            'name': dep['name'], 'specifier': dep.get('specifier', ''),
        })
    with open(os.path.join(output_path, 'direct_by_config_file.json'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(direct_by_file), f, indent=2)

    with open(os.path.join(output_path, 'indirect_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in indirect_dependency_declaration_list:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")
            for parent in dep.get('from_parents', []):
                parent_name = parent.get('name', parent) if isinstance(parent, dict) else parent
                parent_source = parent.get('source', '') if isinstance(parent, dict) else ''
                suffix = f' ({parent_source})' if parent_source else ''
                f.write(f"  # from parent package: {parent_name}{suffix}\n")

    with open(os.path.join(output_path, 'direct_and_indirect_dependency_packages.txt'), 'w', encoding='utf-8') as f:
        for dep in direct_dependency_declarations:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")
        for dep in indirect_dependency_declaration_list:
            if dep.get('specifier'):
                f.write(f"{dep['name']} {dep['specifier']}\n")
            else:
                f.write(f"{dep['name']}\n")

    # 同名包版本对比
    all_same_name_packages = {}
    package_public_private_version = {}
    for package, private_version in project_private_packages.items():
        declared_specifier, constraint_source = get_effective_specifier(
            package, direct_dependency_specifiers, indirect_dependency_specifiers)
        entry, public_version = build_same_name_entry(
            package, private_version,
            declared_specifier=declared_specifier, constraint_source=constraint_source)
        if public_version is None:
            continue
        all_same_name_packages[package] = entry
        if parse(public_version) >= parse(private_version):
            package_public_private_version[package] = entry

    json_direct = {p: all_same_name_packages[p] for p in direct_dependency_package if p in all_same_name_packages}
    json_indirect = {p: all_same_name_packages[p] for p in indirect_dependency_package if p in all_same_name_packages}
    direct_gt = {p: package_public_private_version[p] for p in direct_dependency_package if p in package_public_private_version}
    indirect_gt = {p: package_public_private_version[p] for p in indirect_dependency_package if p in package_public_private_version}

    result_info = {t: {'number': 0, 'packages': {}} for t in types}
    result_info['risk'] = ''
    result_info[types[0]] = {'number': len(direct_dependency_declarations), 'packages': direct_dependency_declarations}
    result_info[types[1]] = {'number': len(indirect_dependency_package), 'packages': indirect_dependency_declaration_list}
    result_info[types[2]] = {'number': len(direct_gt), 'packages': direct_gt}
    result_info[types[3]] = {'number': len(indirect_gt), 'packages': indirect_gt}
    result_info['traversal'] = traversal_stats
    if package_public_private_version:
        result_info['risk'] = 'Critical: Public library has same-name and higher version packages'
    else:
        result_info['risk'] = 'No confusion'

    with open(os.path.join(output_path, 'dependency_confusion.txt'), 'w') as f:
        json.dump(convert_sets_to_lists(result_info), f, indent=5)

    same_name_output = {
        "direct dependency with same name": {"number": len(json_direct), "packages": json_direct},
        "indirect dependency with same name": {"number": len(json_indirect), "packages": json_indirect},
    }
    with open(os.path.join(output_path, 'same_name_dependencies.txt'), 'w', encoding='utf-8') as f:
        json.dump(convert_sets_to_lists(same_name_output), f, indent=4)


def _run_unified_both_modes(project_directory, output_base):
    """一次 BFS 同时生成 expanded/ 和 pruned/ 两个结果目录。"""
    print('======== unified both modes (single BFS) ========')

    # 1. 扫描 + get deps + write file_include_dependency
    private_urls, file_private_sources = scan_private_urls(project_directory)
    print(f"Private URLs: {private_urls}")
    if not private_urls:
        print('No dependency confusion !!!')
        expanded_out = os.path.join(output_base, TRAVERSE_MODE_EXPANDED)
        pruned_out = os.path.join(output_base, TRAVERSE_MODE_PRUNED)
        os.makedirs(expanded_out, exist_ok=True)
        os.makedirs(pruned_out, exist_ok=True)
        _write_private_sources(expanded_out, project_directory, file_private_sources)
        _write_private_sources(pruned_out, project_directory, file_private_sources)
        return

    # file_include_dependency needs a temp dir for get_dependency
    expanded_out = os.path.join(output_base, TRAVERSE_MODE_EXPANDED)
    pruned_out = os.path.join(output_base, TRAVERSE_MODE_PRUNED)
    os.makedirs(expanded_out, exist_ok=True)
    os.makedirs(pruned_out, exist_ok=True)

    # BFS 前先写出 private_sources.txt（不依赖 BFS 结果）
    _write_private_sources(expanded_out, project_directory, file_private_sources)
    _write_private_sources(pruned_out, project_directory, file_private_sources)

    direct_dependency_declarations = get_dependency(project_directory, expanded_out)
    direct_dependency_package = [dep['name'] for dep in direct_dependency_declarations]
    direct_dependency_specifiers = {
        dep['name']: dep.get('specifier', '') for dep in direct_dependency_declarations
    }

    # 2. BFS expanded (一次)
    sorted_private_urls = sorted(private_urls)
    print(f'Private indexes: {len(sorted_private_urls)}')
    (project_private_package_exp, indirect_specs_exp, indirect_rows_exp,
     traversal_stats_exp, bfs_edges_exp, package_details_exp) = analyze_project_dependencies(
        direct_dependency_package, sorted_private_urls,
        prune_public_branches=False, direct_specifiers=direct_dependency_specifiers)

    # Merge (same as original judge function)
    project_private_packages_exp = dict(project_private_package_exp)
    indirect_dependency_specifiers_exp = {}
    indirect_dependency_declarations_exp = {}
    for name, spec in indirect_specs_exp.items():
        indirect_dependency_specifiers_exp[name] = merge_specifiers(
            indirect_dependency_specifiers_exp.get(name, ''), spec)
    for name, row in indirect_rows_exp.items():
        if name in indirect_dependency_declarations_exp:
            existing = indirect_dependency_declarations_exp[name]
            existing['specifier'] = indirect_dependency_specifiers_exp[name]
            existing['raw_requirement'] = f"{existing['raw_requirement']} ; {row['raw_requirement']}"
            for parent in row.get('from_parents', []):
                if not parent_already_listed(existing['from_parents'], parent):
                    existing['from_parents'].append(parent)
        else:
            indirect_dependency_declarations_exp[name] = row
    for name in indirect_dependency_specifiers_exp:
        if name in indirect_dependency_declarations_exp:
            indirect_dependency_declarations_exp[name]['specifier'] = indirect_dependency_specifiers_exp[name]

    # 3. 推导 pruned
    (project_private_package_pruned, indirect_specs_pruned, indirect_rows_pruned,
     traversal_stats_pruned, bfs_edges_pruned, package_details_pruned) = derive_pruned_results(
        bfs_edges_exp, project_private_package_exp, direct_dependency_package,
        indirect_dependency_specifiers_exp, indirect_dependency_declarations_exp,
        package_details_exp)

    project_private_packages_pruned = dict(project_private_package_pruned)
    indirect_dependency_specifiers_pruned = {}
    indirect_dependency_declarations_pruned = {}
    for name, spec in indirect_specs_pruned.items():
        indirect_dependency_specifiers_pruned[name] = spec
    for name, row in indirect_rows_pruned.items():
        indirect_dependency_declarations_pruned[name] = row
    for name in indirect_dependency_specifiers_pruned:
        if name in indirect_dependency_declarations_pruned:
            indirect_dependency_declarations_pruned[name]['specifier'] = indirect_dependency_specifiers_pruned[name]

    # 4. 写出 expanded/
    print(f'Writing expanded/ -> {expanded_out}')
    _write_bfs_outputs(expanded_out, project_directory,
                       project_private_packages_exp, direct_dependency_declarations,
                       direct_dependency_package, direct_dependency_specifiers,
                       indirect_dependency_specifiers_exp, indirect_dependency_declarations_exp,
                       traversal_stats_exp, bfs_edges_exp, package_details_exp,
                       file_private_sources)

    # 5. 写出 pruned/
    print(f'Writing pruned/ -> {pruned_out}')
    _write_bfs_outputs(pruned_out, project_directory,
                       project_private_packages_pruned, direct_dependency_declarations,
                       direct_dependency_package, direct_dependency_specifiers,
                       indirect_dependency_specifiers_pruned, indirect_dependency_declarations_pruned,
                       traversal_stats_pruned, bfs_edges_pruned, package_details_pruned,
                       file_private_sources)

    print('Done.')


def resolve_mode_output_path(output_base, prune_public_branches, split_mode_output=True):
    """
    将输出根目录解析为当前遍历模式对应的子目录。

    split_mode_output=True（默认）时：
      - expanded → {output_base}/expanded
      - pruned   → {output_base}/pruned
    split_mode_output=False 时：直接使用 output_base（兼容旧用法）。
    """
    if not split_mode_output:
        return output_base
    mode_dir = TRAVERSE_MODE_PRUNED if prune_public_branches else TRAVERSE_MODE_EXPANDED
    return os.path.join(output_base, mode_dir)


def run_dependency_confusion_analysis(
    project_directory,
    output_base,
    prune_public_branches=False,
    split_mode_output=True,
):
    output_path = resolve_mode_output_path(
        output_base,
        prune_public_branches=prune_public_branches,
        split_mode_output=split_mode_output,
    )
    mode_label = 'pruned_public_branches' if prune_public_branches else 'mixed_source_expanded'
    print(f'Traversal mode: {mode_label}')
    print(f'Output directory: {output_path}')
    os.makedirs(output_path, exist_ok=True)
    judge_dependency_confusion(
        project_directory,
        output_path,
        prune_public_branches=prune_public_branches,
    )
    return output_path


if __name__=='__main__':
    parser = argparse.ArgumentParser(
        description='分析项目依赖混淆情况（默认 expanded / pruned 结果分别写入子目录）',
    )
    parser.add_argument('project_directory', nargs='?', default='', help='project directory')
    parser.add_argument(
        'output_path',
        nargs='?',
        default='',
        help='输出根目录；默认会在其下创建 expanded/ 或 pruned/ 子目录',
    )
    parser.add_argument(
        '--prune-public-branches',
        action='store_true',
        help='legacy 模式：在 public-only 节点停止 BFS（结果写入 pruned/）',
    )
    parser.add_argument(
        '--run-both-modes',
        action='store_true',
        help='一次运行两种模式，结果分别写入 {output_path}/expanded 与 {output_path}/pruned',
    )
    parser.add_argument(
        '--no-split-mode-output',
        action='store_true',
        help='不创建 expanded/pruned 子目录，直接写入 output_path（旧行为）',
    )
    args = parser.parse_args()
    split_mode_output = not args.no_split_mode_output

    if args.project_directory and args.output_path:
        project_directory = args.project_directory
        output_base = args.output_path
    else:
        d = r'roop-main'
        path = r'/home/lab314/zxy/dependncy_confusion/analysis_program'
        output_path_dir = r'/home/lab314/zxy/dependncy_confusion/comparison_results_consider_version_0711'
        project_directory = os.path.join(path, d)
        output_base = os.path.join(output_path_dir, d)

    if args.run_both_modes:
        _run_unified_both_modes(project_directory, output_base)
    else:
        run_dependency_confusion_analysis(
            project_directory,
            output_base,
            prune_public_branches=args.prune_public_branches,
            split_mode_output=split_mode_output,
        )


# cd /home/lab314/zxy/dependncy_confusion && python -u detect_dependency_confusion.py /home/lab314/zxy/dependncy_confusion/analysis_program/ray-project_ray /home/lab314/zxy/dependncy_confusion/comparison_results/ray-project_ray --run-both-modes




