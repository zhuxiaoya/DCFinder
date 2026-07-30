#!/usr/bin/env python3
"""
对每个含私有源的配置文件执行 pip install --dry-run --report，
然后对每个实际安装的包查询其在公开(PyPI)和私有仓库的存在性与版本。

输出: dynamic_results_all_dryrun/<project>/pip_deps_detail.csv

用法:
  python3 run_pip_dryrun_v2.py                     # 全部项目
  python3 run_pip_dryrun_v2.py --only bmaltais_kohya_ss
  python3 run_pip_dryrun_v2.py --dry-run            # 仅列出待执行任务
  python3 run_pip_dryrun_v2.py --parallel 4
"""

import csv
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from packaging.utils import canonicalize_name
from packaging.version import parse as parse_version

BASE_DIR = Path("/home/lab314/zxy/dependncy_confusion")
ANALYSIS_DIR = BASE_DIR / "analysis_program"
#RESULTS_DIR = BASE_DIR / "dynamic_results_all_dryrun_consider_version"
#DCFINDER_DIR = BASE_DIR / "comparison_results_consider_version"
RESULTS_DIR = BASE_DIR / "dynamic_results_all_dryrun_v4"
DCFINDER_DIR = BASE_DIR / "comparison_results_v4"
TIMEOUT = 3600  # 1 小时，pip install . 解析大型项目可能需要较长时间
PARALLEL = 8

MIRROR_DOMAINS = (
    'tuna.tsinghua.edu.cn', 'mirrors.aliyun.com', 'mirrors.cloud.tencent.com',
    'mirrors.ustc.edu.cn', 'mirrors.huaweicloud.com', 'mirrors.163.com',
    'pypi.douban.com',
)

PUBLIC_HOSTS = ('pypi.org', 'pythonhosted.org', 'python.org') + MIRROR_DOMAINS


def _is_public_url(url):
    return any(h in url.lower() for h in PUBLIC_HOSTS)


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def norm(name):
    if not name: return ""
    return str(name).strip().lower().replace('_', '-').replace('.', '-')


def load_private_urls(project):
    """读 private_sources.txt → 返回私有 URL 列表"""
    sf = DCFINDER_DIR / project / "expanded" / "private_sources.txt"
    if not sf.exists():
        return []
    pat = re.compile(r'(?:index_url|extra_index_url|poetry\.source|pipfile\.source):\s*(https?://\S+)')
    urls, seen = [], set()
    with open(sf) as f:
        for line in f:
            m = pat.search(line)
            if not m: continue
            url = m.group(1).rstrip('/')
            if url in seen: continue
            if 'pypi.org/simple' in url or 'pypi.org/pypi' in url: continue
            if any(d in url for d in MIRROR_DOMAINS): continue
            seen.add(url)
            urls.append(m.group(1))
    return urls


def index_slug(pkg_name):
    return canonicalize_name(pkg_name).replace('_', '-').lower()


def is_pkg_on_index(base_url, pkg_name, timeout=10):
    """探测包是否在某 simple index 上"""
    pkg_url = urljoin(base_url.rstrip('/') + '/', index_slug(pkg_name) + '/')
    try:
        r = requests.get(pkg_url, timeout=timeout, stream=True)
        if r.status_code != 200: return False, None
        chunk = b''
        for part in r.iter_content(4096):
            chunk += part
            if len(chunk) >= 8192: break
        text = chunk.decode('utf-8', errors='ignore').lower()
        if not ('.whl' in text or '.tar.gz' in text):
            return False, None

        # 解析当前页面上版本号最高的文件名
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, 'html.parser')
        best_version = None
        for link in soup.find_all('a', href=True):
            href = link['href']
            if not href.endswith(('.whl', '.tar.gz', '.zip')): continue
            fn = href.rstrip('/')
            m = re.search(r'(\d+(?:\.\d+)*(?:\.?(?:dev|a|b|rc)\d+)?)', fn)
            if m:
                try:
                    v = parse_version(m.group(1))
                    if best_version is None or v > best_version:
                        best_version = v
                except: pass
        return True, str(best_version) if best_version else None
    except:
        return False, None


def get_pypi_info(pkg_name):
    """从 PyPI JSON API 获取最新版本和 yanked 信息"""
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200: return None, False, None
        data = r.json()
        info = data.get('info', {}) or {}
        releases = data.get('releases', {})
        best_version = None
        is_yanked = False
        for ver, files in releases.items():
            try: pv = parse_version(ver)
            except: continue
            if best_version is None or pv > best_version:
                best_version = pv
            if any(f.get('yanked', False) for f in files):
                if best_version is not None and str(best_version) == ver:
                    is_yanked = True
        return info, is_yanked, str(best_version) if best_version else None
    except:
        return None, False, None


# ═══════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════

_cache_private_pkg = {}
_cache_pypi = {}


def resolve_pkg_source_info(pkg_name, private_urls):
    """查询包在私有仓库和 PyPI 的存在性 + 版本，带缓存。"""
    if pkg_name in _cache_private_pkg:
        return _cache_private_pkg[pkg_name]

    # 私有仓库
    private_exists = False
    private_version = None
    private_url_found = None
    for url in private_urls:
        exists, ver = is_pkg_on_index(url, pkg_name)
        if exists:
            private_exists = True
            if ver: private_version = ver
            private_url_found = url
            break

    # PyPI
    _, yanked, pypi_version = get_pypi_info(pkg_name)

    _cache_private_pkg[pkg_name] = (private_exists, private_version, private_url_found,
                                     pypi_version, yanked)
    return _cache_private_pkg[pkg_name]


def build_tasks(only_project=None):
    """从 file_include_dependency.txt 提取完整路径直接使用，不再通过 basename 重新搜索。"""
    tasks = OrderedDict()
    expected_by_project = {}  # project -> set of expected report rnames

    for proj_dir in sorted(ANALYSIS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        if only_project and project != only_project:
            continue

        fp = DCFINDER_DIR / project / "expanded" / "file_include_dependency.txt"
        if not fp.exists():
            continue

        # 直接从 file_include_dependency.txt 提取完整路径
        dcf_config_paths = []
        with open(fp, encoding='utf-8') as f:
            for line in f:
                if line.startswith("# 依赖文件:"):
                    raw = line.split("依赖文件:")[1].strip()
                    if not os.path.isabs(raw):
                        actual_path = os.path.join(str(BASE_DIR), raw)
                    else:
                        actual_path = raw
                    actual_path = os.path.normpath(actual_path)
                    if os.path.exists(actual_path):
                        dcf_config_paths.append(actual_path)

        # 已有的 JSON report
        rdir = RESULTS_DIR / project / "pip_reports"
        existing = {f.name for f in rdir.glob("*.json")} if rdir.exists() else set()

        expected_rnames = set()

        for actual_path in sorted(set(dcf_config_paths)):
            rel = os.path.relpath(actual_path, str(proj_dir))
            basename = os.path.basename(actual_path)
            fname_low = basename.lower()

            if fname_low in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                ctype = 'pyproject' if 'pyproject' in fname_low else 'setup_py' if 'setup.py' in fname_low else 'setup_cfg'
            elif fname_low == 'pipfile':
                continue
            elif basename.endswith('.py'):
                continue
            elif basename.endswith(('.txt', '.pip', '.in')):
                ctype = 'requirement'
            else:
                continue

            flat_rel = rel.replace('/', '__').replace('\\', '__')
            rname = f"{ctype}__{flat_rel}.json"
            expected_rnames.add(rname)

            # 已有则保留，不重新执行
            if rname in existing:
                continue

            if fname_low in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                action, target = 'pip_install_dir', os.path.dirname(actual_path)
            else:
                action, target = 'pip_install_r', actual_path

            key = (project, rname)
            if key not in tasks:
                tasks[key] = (project, actual_path, action, target, rname)

        expected_by_project[project] = expected_rnames

    return list(tasks.values()), expected_by_project


def cleanup_stale_reports(expected_by_project, only_project=None):
    """删除 file_include_dependency.txt 中不存在的配置文件对应的 pip report 和 failure 记录。"""
    import shutil

    for project, expected_rnames in expected_by_project.items():
        if only_project and project != only_project:
            continue
        rdir = RESULTS_DIR / project / "pip_reports"
        if not rdir.exists():
            continue

        # 1. 删除多余的 JSON report
        deleted_reports = 0
        for jf in list(rdir.glob("*.json")):
            if jf.name not in expected_rnames:
                jf.unlink()
                deleted_reports += 1
                tmp_dir = rdir / f".tmp_{jf.name.replace('.json', '')}"
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        # 2. 清理 pip_failures.csv：只保留属于 file_include_dependency.txt 的条目
        removed_failures = 0
        failure_csv = rdir / "pip_failures.csv"
        if failure_csv.exists():
            fp = DCFINDER_DIR / project / "expanded" / "file_include_dependency.txt"
            valid_paths, valid_dirs = set(), set()
            if fp.exists():
                with open(fp, encoding='utf-8') as f:
                    for line in f:
                        if line.startswith("# 依赖文件:"):
                            raw = line.split("依赖文件:")[1].strip()
                            if not os.path.isabs(raw):
                                raw = os.path.join(str(BASE_DIR), raw)
                            raw = os.path.normpath(raw)
                            valid_paths.add(raw)
                            valid_dirs.add(os.path.dirname(raw))

            rows_keep = []
            with open(failure_csv, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    cf = row.get('config_file', '')
                    cf_norm = os.path.normpath(cf) if cf else ''
                    if cf_norm in valid_paths or cf_norm in valid_dirs:
                        rows_keep.append(row)
                    else:
                        removed_failures += 1

            if removed_failures > 0:
                with open(failure_csv, 'w', newline='', encoding='utf-8') as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(rows_keep)

        # 3. 重新生成项目 CSV（过滤掉已删除 report 对应的行）
        if deleted_reports > 0:
            project_csv = RESULTS_DIR / project / "pip_deps_detail.csv"
            if project_csv.exists():
                rows_keep = []
                with open(project_csv, newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    fns = reader.fieldnames
                    for row in reader:
                        if row.get('config_file', '') in expected_rnames:
                            rows_keep.append(row)
                with open(project_csv, 'w', newline='', encoding='utf-8') as f:
                    w = csv.DictWriter(f, fieldnames=fns)
                    w.writeheader()
                    w.writerows(rows_keep)

            msg = f"[CLEANUP] {project}: deleted {deleted_reports} stale report(s)"
            if removed_failures > 0:
                msg += f", {removed_failures} failure entries"
            print(msg)


def parse_requirements_lines(filepath):
    """逐行解析 requirements.txt，返回 (packages, failures)。"""
    import re as _re
    packages = []
    failures = []
    if not os.path.exists(filepath):
        return packages, failures
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if stripped.startswith(('--index-url', '--extra-index-url', '--find-links',
                                     '-i ', '-f ', '--trusted-host', '--no-index',
                                     '--pre', '--no-deps', '--only-binary',
                                     '--prefer-binary', '--require-hashes')):
                continue
            if stripped.startswith(('-r ', '-c ', '--requirement ', '--constraint ')):
                continue
            if stripped.startswith('${') or stripped.startswith('$'):
                continue
            if any(stripped.startswith(p) for p in ('git+', 'svn+', 'hg+', 'bzr+')):
                failures.append((stripped, f'Line {lineno}: VCS依赖'))
                continue
            if _re.match(r'^https?://', stripped):
                failures.append((stripped, f'Line {lineno}: 裸URL'))
                continue
            if _re.match(r'^[\w.\-]+[><=~!].*\s+[\w.\-]+[><=~!]', stripped):
                failures.append((stripped, f'Line {lineno}: 一行多包'))
                continue
            pkg_clean = _re.sub(r'\[.*?\]', '', stripped)
            # 去掉 PEP 440 本地版本标识符（+cu118, +cpu 等），
            # 因为清华镜像不托管这些后缀的 wheel
            pkg_clean = _re.sub(r'\+[a-zA-Z0-9]+', '', pkg_clean)
            pkg_line = pkg_clean.split(';')[0].strip()
            pkg_name = None
            for op in ['>=', '<=', '>', '<', '~=', '==', '!=']:
                if op in pkg_line:
                    pkg_name = pkg_line.split(op)[0].strip()
                    break
            if pkg_name is None:
                pkg_name = pkg_line.strip()
            if not pkg_name or not _re.match(r'^[a-zA-Z0-9][\w.\-]*$', pkg_name):
                failures.append((stripped, f'Line {lineno}: 无法解析包名'))
                continue
            packages.append((pkg_clean, pkg_name))
    return packages, failures


def merge_install_reports(report_paths):
    """合并多个 pip --report JSON，按包名去重。"""
    merged = {"install": [], "pip_version": "", "environment": {}}
    seen = set()
    for rp in report_paths:
        if not rp.exists() or os.path.getsize(rp) < 10: continue
        try:
            with open(rp) as f: data = json.load(f)
        except: continue
        if not merged["pip_version"] and data.get("pip_version"):
            merged["pip_version"] = data["pip_version"]
        if not merged["environment"] and data.get("environment"):
            merged["environment"] = data["environment"]
        for item in data.get("install", []):
            nm = norm(item.get("metadata", {}).get("name", ""))
            if nm and nm not in seen:
                seen.add(nm)
                merged["install"].append(item)
            elif not nm:
                merged["install"].append(item)
    return merged


def run_one_config(project, actual_path, action, target, rname):
    """执行单个配置文件，返回 pip report JSON path 和失败信息列表。"""
    rdir = RESULTS_DIR / project / "pip_reports"
    rdir.mkdir(parents=True, exist_ok=True)
    report_path = rdir / rname

    private_urls = load_private_urls(project)
    idx = ['--index-url', 'https://pypi.org/simple/']
    for u in private_urls:
        idx.extend(['--extra-index-url', u])
    idx_str = ' '.join(idx)

    failures = []

    if action == 'pip_install_r':
        # ── 策略1: 全文件执行 ──
        cmd = f"pip install --dry-run --report '{report_path}' {idx_str} --ignore-installed -r '{target}'"
        try:
            subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=TIMEOUT,
                           env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
            if report_path.exists() and report_path.stat().st_size > 10:
                return report_path, None
        except: pass

        # ── 策略2: 逐包执行 ──
        packages, parse_failures = parse_requirements_lines(target)
        if not packages and not parse_failures:
            failures.append({'config_file': target, 'failed_package': '',
                           'reason': '文件无有效依赖', 'mode': '-r'})
            return None, failures

        for pf in parse_failures:
            failures.append({'config_file': target, 'failed_package': pf[0][:80].strip(),
                           'reason': pf[1], 'mode': '-r'})

        tmp_dir = rdir / f".tmp_{rname.replace('.json', '')}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_reports = []

        for pkg_line, pkg_name in packages:
            tmp_report = tmp_dir / f"{norm(pkg_name)}.json"
            cmd_pkg = f"pip install --dry-run --report '{tmp_report}' {idx_str} --ignore-installed --no-deps '{pkg_line}'"
            ok = False
            try:
                subprocess.run(cmd_pkg, shell=True, capture_output=True, text=True,
                               timeout=120,
                               env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
                if tmp_report.exists() and os.path.getsize(tmp_report) > 10:
                    temp_reports.append(tmp_report)
                    ok = True
                    # also transitive deps
                    tmp_full = tmp_dir / f"{norm(pkg_name)}_full.json"
                    try:
                        subprocess.run(
                            f"pip install --dry-run --report '{tmp_full}' {idx_str} --ignore-installed '{pkg_line}'",
                            shell=True, capture_output=True, text=True, timeout=120,
                            env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
                        if tmp_full.exists() and os.path.getsize(tmp_full) > 10:
                            temp_reports.append(tmp_full)
                    except: pass
            except: pass

            if not ok:
                failures.append({'config_file': target, 'failed_package': pkg_name,
                               'reason': 'pip解析失败', 'mode': '-r'})

        if temp_reports:
            merged = merge_install_reports(temp_reports)
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(merged, f)
            return report_path, (failures if failures else None)

        return None, (failures if failures else None)

    else:
        # ── pip install . (setup.py / pyproject.toml / setup.cfg) ──
        # 策略1: 全项目执行
        cmd = f"pip install --dry-run --report '{report_path}' {idx_str} --ignore-installed '{target}'"
        try:
            subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=TIMEOUT,
                           cwd=os.path.dirname(actual_path),
                           env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
            if report_path.exists() and report_path.stat().st_size > 10:
                return report_path, None
        except: pass

        # ── 策略2: 逐包执行 (pip install . 失败时的回退) ──
        # 从 pyproject.toml / setup.py / setup.cfg 解析依赖列表
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from detect_dependency_confusion_v4 import (
            get_pyproject_toml_dependencies,
            get_setup_cfg_dependencies,
        )
        import analysis_setup_py

        fname_low = os.path.basename(actual_path).lower()
        file_dependencies = []
        if fname_low == 'pyproject.toml':
            file_dependencies = get_pyproject_toml_dependencies(actual_path)
        elif fname_low == 'setup.py':
            file_dependencies = analysis_setup_py.get_setup_py_dependencies(actual_path)
        elif fname_low == 'setup.cfg':
            file_dependencies = get_setup_cfg_dependencies(actual_path)

        if not file_dependencies:
            return None, None

        tmp_dir = rdir / f".tmp_{rname.replace('.json', '')}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_reports = []

        for dep in file_dependencies:
            dep_name = dep.get('name', '')
            dep_spec = dep.get('specifier', '')
            if not dep_name:
                continue
            pkg_line = f"{dep_name}{dep_spec}" if dep_spec else dep_name

            # --no-deps + --ignore-installed: 确保从零解析且当前包标为 requested=True
            tmp_no_deps = tmp_dir / f"{norm(dep_name)}.json"
            try:
                subprocess.run(
                    f"pip install --dry-run --report '{tmp_no_deps}' {idx_str} --ignore-installed --no-deps '{pkg_line}'",
                    shell=True, capture_output=True, text=True, timeout=120,
                    env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
                if tmp_no_deps.exists() and os.path.getsize(tmp_no_deps) > 10:
                    temp_reports.append(tmp_no_deps)
            except: pass

            # 完整依赖 + --ignore-installed: 收集该包的传递依赖
            tmp_full = tmp_dir / f"{norm(dep_name)}_full.json"
            try:
                subprocess.run(
                    f"pip install --dry-run --report '{tmp_full}' {idx_str} --ignore-installed '{pkg_line}'",
                    shell=True, capture_output=True, text=True, timeout=120,
                    env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
                if tmp_full.exists() and os.path.getsize(tmp_full) > 10:
                    temp_reports.append(tmp_full)
            except: pass

        if temp_reports:
            merged = merge_install_reports(temp_reports)
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(merged, f)
            return report_path, None

        return None, None


def analyze_one_report(report_path, project):
    """分析单个 pip report，产出行列表。"""
    with open(report_path) as f:
        data = json.load(f)
    install = data.get('install', [])
    if not install: return []

    private_urls = load_private_urls(project)
    rows = []

    has_file_url = any('file://' in it.get('download_info', {}).get('url', '') for it in install)

    if has_file_url:
        # pip install . → 项目自身需特殊处理
        project_direct_names = set()
        installed_map = {}
        for item in install:
            url = item.get('download_info', {}).get('url', '')
            meta = item.get('metadata', {})
            nm = norm(meta.get('name', ''))
            if 'file://' in url and item.get('requested'):
                for req_line in meta.get('requires_dist', []):
                    req_part = req_line.split(';')[0].strip()
                    for op in ['>=', '<=', '>', '<', '~=', '==', '!=', '[']:
                        if op in req_part: req_part = req_part.split(op)[0].strip(); break
                    dn = norm(req_part)
                    if dn: project_direct_names.add(dn)
            elif nm and 'file://' not in url:
                installed_map[nm] = (meta.get('version', ''), url, meta.get('requires_dist', []),
                                     item.get('requested', False))

        for nm, (ver, url, rd, req_flag) in installed_map.items():
            is_direct = nm in project_direct_names
            is_private = not _is_public_url(url)

            # 查询公私仓库信息
            priv_exist, priv_ver, _, pypi_ver, yanked = resolve_pkg_source_info(nm, private_urls)

            rows.append({
                'project': project,
                'config_file': os.path.basename(str(report_path)),
                'package': nm,
                'installed_version': ver,
                'download_source': 'private' if is_private else 'pypi',
                'download_url': url,
                'is_direct': is_direct,
                'is_yanked': yanked,
                'pypi_latest_version': pypi_ver or '',
                'private_exists': priv_exist,
                'private_latest_version': priv_ver or '',
            })
    else:
        # pip install -r
        for item in install:
            meta = item.get('metadata', {})
            nm = norm(meta.get('name', ''))
            if not nm: continue
            ver = meta.get('version', '')
            url = item.get('download_info', {}).get('url', '')
            is_direct = item.get('requested', False)
            is_private = not _is_public_url(url)

            priv_exist, priv_ver, _, pypi_ver, yanked = resolve_pkg_source_info(nm, private_urls)

            rows.append({
                'project': project,
                'config_file': os.path.basename(str(report_path)),
                'package': nm,
                'installed_version': ver,
                'download_source': 'private' if is_private else 'pypi',
                'download_url': url,
                'is_direct': is_direct,
                'is_yanked': yanked,
                'pypi_latest_version': pypi_ver or '',
                'private_exists': priv_exist,
                'private_latest_version': priv_ver or '',
            })

    return rows


_print_lock = threading.Lock()


def run_all(only_project=None, dry_run=False):
    tasks, expected_by_project = build_tasks(only_project)

    if dry_run:
        # 显示将要清理的过期 report
        for project, expected_rnames in expected_by_project.items():
            rdir = RESULTS_DIR / project / "pip_reports"
            if not rdir.exists():
                continue
            stale = [f.name for f in rdir.glob("*.json") if f.name not in expected_rnames]
            if stale:
                print(f"[DRY-RUN] {project}: would delete {len(stale)} stale report(s)")
        for _, (project, ap, action, target, rname) in enumerate(tasks):
            print(f"[DRY-RUN] {project:<44} {os.path.basename(ap):<30} → {rname}")
        return

    # 清理 file_include_dependency.txt 中不存在的配置文件对应的结果
    cleanup_stale_reports(expected_by_project, only_project)

    total = len(tasks)
    print(f"任务总数: {total}, 并行度: {PARALLEL}")
    done = success = fail = 0
    all_rows = []

    def _run(task_tuple):
        nonlocal done, success, fail, all_rows
        idx, (project, actual_path, action, target, rname) = task_tuple
        basename = os.path.basename(actual_path)

        report_path, task_failures = run_one_config(project, actual_path, action, target, rname)
        if report_path:
            rows = analyze_one_report(report_path, project)
            with _print_lock:
                done += 1; success += 1
                all_rows.extend(rows)
                fail_note = f" [{len(task_failures)}包失败]" if task_failures else ""
                print(f"[{done}/{total}] {project:<44} {basename:<30} ✅ {len(rows)} packages{fail_note}")
                if rows:
                    _write_project_csv(project)
        else:
            with _print_lock:
                done += 1; fail += 1
                fail_pkgs = ','.join(f['failed_package'] for f in (task_failures or [])[:3]) if task_failures else "?"
                print(f"[{done}/{total}] {project:<44} {basename:<30} ❌ FAILED [{fail_pkgs}]")

        # 写失败记录
        if task_failures:
            failure_csv = RESULTS_DIR / project / "pip_reports" / "pip_failures.csv"
            with _print_lock:
                f_exists = failure_csv.exists()
            with open(failure_csv, 'a', newline='', encoding='utf-8') as ff:
                w = csv.DictWriter(ff, fieldnames=['config_file', 'failed_package', 'reason', 'mode'])
                if not f_exists: w.writeheader()
                w.writerows(task_failures)

    if total == 0:
        print("没有待执行任务。")
        _write_all_csv()
        return

    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futures = [ex.submit(_run, (i, t)) for i, t in enumerate(tasks)]
        for _ in as_completed(futures): pass

    print(f"\n完成: 成功={success} 失败={fail}")
    _write_all_csv()


# ═══════════════════════════════════════════════════════════════
# CSV 输出
# ═══════════════════════════════════════════════════════════════

FIELDNAMES = ['project', 'config_file', 'package', 'installed_version',
              'download_source', 'download_url', 'is_direct', 'is_yanked',
              'pypi_latest_version', 'private_exists', 'private_latest_version']


def _write_project_csv(project, rows_override=None):
    """读该项目的所有 pip report JSON 并生成 pip_deps_detail.csv。"""
    if rows_override is not None:
        rows = rows_override
    else:
        rows = []
        rdir = RESULTS_DIR / project / "pip_reports"
        if not rdir.exists(): return
        for rf in sorted(rdir.glob("*.json")):
            if rf.stat().st_size < 10: continue
            rows.extend(analyze_one_report(rf, project))

    if not rows: return
    # 合并去重 (按 project+package)
    seen = set()
    deduped = []
    for r in rows:
        key = (r['project'], r['package'], r['config_file'])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    csv_path = RESULTS_DIR / project / "pip_deps_detail.csv"
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(deduped)

    # 统计
    direct_priv = sum(1 for r in deduped if r['is_direct'] and r['download_source'] == 'private')
    direct_pub = sum(1 for r in deduped if r['is_direct'] and r['download_source'] == 'pypi')
    ind_priv = sum(1 for r in deduped if not r['is_direct'] and r['download_source'] == 'private')
    ind_pub = sum(1 for r in deduped if not r['is_direct'] and r['download_source'] == 'pypi')
    both_exist = sum(1 for r in deduped if r['private_exists'] and r['pypi_latest_version'])
    confusion = sum(1 for r in deduped if r['private_exists'] and r['pypi_latest_version']
                    and parse_version(str(r['pypi_latest_version'])) > parse_version(str(r['installed_version'])))

    # 写统计摘要
    summary_path = RESULTS_DIR / project / "pip_deps_summary.txt"
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(f"# {project} pip dry-run 依赖分析摘要\n\n")
        f.write(f"总包数: {len(deduped)}\n\n")
        f.write(f"直接依赖 - 私有源: {direct_priv}\n")
        f.write(f"直接依赖 - 公开源: {direct_pub}\n")
        f.write(f"间接依赖 - 私有源: {ind_priv}\n")
        f.write(f"间接依赖 - 公开源: {ind_pub}\n")
        f.write(f"\n公私仓库均存在: {both_exist}\n")
        f.write(f"公开版本高于安装版本 (混淆风险): {confusion}\n")

    return deduped


def _write_all_csv():
    """遍历所有项目，汇总已有 pip_deps_detail.csv"""
    if not RESULTS_DIR.exists():
        print("输出目录尚不存在，跳过全量汇总")
        return
    all_rows = []
    for proj_dir in sorted(RESULTS_DIR.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name in ('venvs', 'pip_logs'): continue
        project = proj_dir.name
        # 读已有的 CSV，不重新做 API 查询
        csv_f = RESULTS_DIR / project / "pip_deps_detail.csv"
        if csv_f.exists():
            with open(csv_f, newline='', encoding='utf-8') as f:
                all_rows.extend(list(csv.DictReader(f)))
        else:
            # 还没生成，尝试从 JSON 生成
            rows = _write_project_csv(project)
            if rows: all_rows.extend(rows)

    if not all_rows: return
    csv_path = RESULTS_DIR / "all_deps_detail.csv"
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(all_rows)
    print(f"全量汇总: {csv_path} ({len(all_rows)} packages)")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    only = None; dry_run = False
    args = sys.argv[1:]; i = 0
    while i < len(args):
        if args[i] == '--only' and i+1 < len(args): only = args[i+1]; i += 2
        elif args[i] == '--dry-run': dry_run = True; i += 1
        elif args[i] == '--parallel' and i+1 < len(args): PARALLEL = int(args[i+1]); i += 2
        else: i += 1
    run_all(only_project=only, dry_run=dry_run)
