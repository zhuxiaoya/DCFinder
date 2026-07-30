#!/usr/bin/env python3
"""
supplement_indirect_gt.py — 对补充GT中的直接依赖运行 pip install --dry-run (带版本约束)
获取间接依赖 (is_direct=False)，追加到 supplementary_gt.csv

策略:
  A. 非编译类别 (setup_py, pyproject, jax_plugins, vcs_url, pkg_fail):
     → 重新解析原配置文件获取带版本约束的直接依赖
     → pip dry-run 安装这些依赖
     → is_direct=False = 间接依赖

  B. 编译类别 (hash, byod, relpath):
     → 从 DCFinder direct_by_config_file.json 获取项目真实直接依赖 (非编译文件)
     → pip dry-run 安装这些依赖
     → is_direct=False = 间接依赖 (分配给该项目的所有编译配置文件)
"""

import os, re, csv, json, ast, subprocess, tempfile, shutil, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Python 3.10 没有 tomllib
try:
    import tomllib
except ImportError:
    import tomli as tomllib

BASE = Path(__file__).resolve().parent
ANALYSIS = BASE / "analysis_program"
RESULTS = BASE / "dynamic_results_all_dryrun_v4"
DC_RESULTS = BASE / "comparison_results_v4"

# ── 复用 supplement_pip_gt.py 的 MISSING_FILES 和解析函数 ──
MISSING_FILES = [
    # F. jax_plugins (4)
    ("jax-main", "jax_plugins/cuda/setup.py", "setup_py__jax_plugins__cuda__setup.py.json", "jax_plugins"),
    ("jax-main", "jax_plugins/cuda/pyproject.toml", "pyproject__jax_plugins__cuda__pyproject.toml.json", "jax_plugins"),
    ("jax-main", "jax_plugins/rocm/setup.py", "setup_py__jax_plugins__rocm__setup.py.json", "jax_plugins"),
    ("jax-main", "jax_plugins/rocm/pyproject.toml", "pyproject__jax_plugins__rocm__pyproject.toml.json", "jax_plugins"),
    # D. pyproject.toml (5)
    ("ipex-llm-main", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("ray-project_ray", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("vllm-main", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("vllm-main", "benchmarks/pyproject.toml", "pyproject__benchmarks__pyproject.toml.json", "pyproject"),
    ("vllm-main", "examples/pyproject.toml", "pyproject__examples__pyproject.toml.json", "pyproject"),
    # E. setup.py (1)
    ("jantic_DeOldify", "setup.py", "setup_py__setup.py.json", "setup_py"),
    # G. VCS URL (1)
    ("OlafenwaMoses_ImageAI", "requirements_extra.txt", "requirement__requirements_extra.txt.json", "vcs_url"),
    # H. 包解析失败 (2)
    ("examples-main", "distributed/rpc/pipeline/requirements.txt",
     "requirement__distributed__rpc__pipeline__requirements.txt.json", "pkg_fail"),
    ("ray-project_ray", "python/ray/util/collective/requirements.txt",
     "requirement__python__ray__util__collective__requirements.txt.json", "pkg_fail"),
    # C. 相对路径文件 (2)
    ("ray-project_ray", "release/requirements-doc.txt",
     "requirement__release__requirements-doc.txt.json", "relpath"),
    ("ray-project_ray", "release/ray_release/byod/requirements_compiled.txt",
     "requirement__release__ray_release__byod__requirements_compiled.txt.json", "relpath"),
    # B. byod 文件 (2)
    ("ray-project_ray", "release/ray_release/byod/requirements_ml_byod_3.9.txt",
     "requirement__release__ray_release__byod__requirements_ml_byod_3.9.txt.json", "byod"),
    ("ray-project_ray", "release/ray_release/byod/requirements_byod_3.9.txt",
     "requirement__release__ray_release__byod__requirements_byod_3.9.txt.json", "byod"),
    # A. hash 锁定文件 (12)
    ("ray-project_ray", "python/requirements_compiled_ray_py311_cpu.txt",
     "requirement__python__requirements_compiled_ray_py311_cpu.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_ray_py311_cu124.txt",
     "requirement__python__requirements_compiled_ray_py311_cu124.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_ray_py311_cu121.txt",
     "requirement__python__requirements_compiled_ray_py311_cu121.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_py311_cpu.txt",
     "requirement__python__requirements_compiled_rayllm_py311_cpu.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_py311_cu121.txt",
     "requirement__python__requirements_compiled_rayllm_py311_cu121.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_py311_cu124.txt",
     "requirement__python__requirements_compiled_rayllm_py311_cu124.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_test_py311_cpu.txt",
     "requirement__python__requirements_compiled_rayllm_test_py311_cpu.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_test_py311_cu121.txt",
     "requirement__python__requirements_compiled_rayllm_test_py311_cu121.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_rayllm_test_py311_cu124.txt",
     "requirement__python__requirements_compiled_rayllm_test_py311_cu124.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_ray_test_py311_cpu.txt",
     "requirement__python__requirements_compiled_ray_test_py311_cpu.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_ray_test_py311_cu121.txt",
     "requirement__python__requirements_compiled_ray_test_py311_cu121.txt.json", "hash"),
    ("ray-project_ray", "python/requirements_compiled_ray_test_py311_cu124.txt",
     "requirement__python__requirements_compiled_ray_test_py311_cu124.txt.json", "hash"),
]

NON_COMPILED_CATS = {"setup_py", "pyproject", "jax_plugins", "vcs_url", "pkg_fail"}
COMPILED_CATS = {"hash", "byod", "relpath"}


# ═══════════════════════════════════════════════════════════════════
#  解析函数 (返回带版本约束的依赖列表, 如 ["click==8.1.7", "ruff>=0.11.5"])
# ═══════════════════════════════════════════════════════════════════

def _strip_specifier(spec):
    """清理版本约束: 去掉 extras 和环境标记"""
    spec = spec.split(";")[0].strip()  # 去环境标记
    spec = re.sub(r'\[.*?\]', '', spec)  # 去 extras
    return spec.strip()


def parse_pyproject_deps_with_version(filepath):
    """解析 pyproject.toml, 返回带版本的依赖列表"""
    deps = []
    try:
        with open(filepath, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return deps

    # [project.dependencies]
    for d in data.get("project", {}).get("dependencies", []):
        spec = _strip_specifier(d)
        if spec:
            deps.append(spec)

    # [project.optional-dependencies]
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        for d in group:
            spec = _strip_specifier(d)
            if spec:
                deps.append(spec)

    # [tool.poetry.dependencies]
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, ver in poetry.items():
        if name.lower() == "python":
            continue
        if isinstance(ver, str):
            deps.append(f"{name}{ver}")
        elif isinstance(ver, dict) and "version" in ver:
            deps.append(f"{name}{ver['version']}")
        elif isinstance(ver, list):
            for v in ver:
                if isinstance(v, str):
                    deps.append(f"{name}{v}" if not v.startswith(("~", ">", "<", "=", "!")) else f"{name}{v}")

    return list(dict.fromkeys(deps))  # 去重保序


def parse_pyproject_with_setup_fallback(filepath):
    """解析 pyproject.toml，如果 [project.dependencies] 为空，回退到同目录 setup.py"""
    deps = parse_pyproject_deps_with_version(filepath)
    if deps:
        return deps
    # pyproject.toml 无依赖，检查是否有 dynamic = ["dependencies"]
    try:
        with open(filepath, "rb") as f:
            data = tomllib.load(f)
        dynamic = data.get("project", {}).get("dynamic", [])
        if "dependencies" in dynamic or not data.get("project", {}).get("dependencies"):
            # 回退到同目录 setup.py
            setup_py = Path(filepath).parent / "setup.py"
            if setup_py.exists():
                deps = parse_setup_py_deps_with_version(setup_py)
    except Exception:
        pass
    return deps


def _ast_node_to_value(node):
    """AST 节点转 Python 值"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Str,)):
        return node.s
    if isinstance(node, ast.List):
        return [_ast_node_to_value(e) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return [_ast_node_to_value(e) for e in node.elts]
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.JoinedStr):
        parts = []
        for val in node.values:
            if isinstance(val, ast.Constant):
                parts.append(str(val.value))
            elif isinstance(val, ast.FormattedValue):
                parts.append(f"{{{val.conversion}}}")
        return "".join(parts)
    return None


def parse_setup_py_deps_with_version(filepath):
    """AST 解析 setup.py, 返回带版本的依赖列表"""
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return []

    deps = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 检查是否是 setup() 调用
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name != "setup":
            continue

        for kw in node.keywords:
            if kw.arg in ("install_requires", "setup_requires"):
                val = _ast_node_to_value(kw.value)
                if isinstance(val, list):
                    for d in val:
                        if isinstance(d, str):
                            spec = _strip_specifier(d)
                            if spec:
                                deps.append(spec)
            elif kw.arg == "extras_require":
                val = _ast_node_to_value(kw.value)
                if isinstance(val, dict):
                    for group_deps in val.values():
                        if isinstance(group_deps, list):
                            for d in group_deps:
                                if isinstance(d, str):
                                    spec = _strip_specifier(d)
                                    if spec:
                                        deps.append(spec)

    # 如果 AST 没找到, 尝试读取引用的 requirements.txt
    if not deps:
        try:
            with open(filepath, encoding="utf-8") as f:
                source = f.read()
            # 查找 open('requirements.txt').read() 模式
            match = re.search(r"open\(['\"]([^'\"]+\.txt)['\"]\)", source)
            if match:
                req_file = match.group(1)
                req_path = Path(filepath).parent / req_file
                if req_path.exists():
                    return extract_packages_from_req_with_version(req_path)
        except Exception:
            pass

    return list(dict.fromkeys(deps))


def extract_vcs_packages_with_version(filepath):
    """从 VCS URL 提取包名 (无法带版本约束)"""
    pkgs = []
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # pkg@git+https://... 或 git+https://...#egg=pkg
            m = re.match(r'^([a-zA-Z0-9][\w.\-]*)@', line)
            if m:
                pkgs.append(m.group(1))
            else:
                m = re.search(r'#egg=([a-zA-Z0-9][\w.\-]*)', line)
                if m:
                    pkgs.append(m.group(1))
    return pkgs


def extract_packages_from_req_with_version(filepath):
    """从 requirements.txt 提取带版本的包名"""
    pkgs = []
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-") or line.startswith("git+") or line.startswith("http"):
                continue
            # 去掉行尾注释
            line = line.split("#")[0].strip()
            if not line:
                continue
            # 去掉 -e 前缀
            line = re.sub(r'^-e\s+', '', line)
            # 去掉路径引用
            if "/" in line and not re.match(r'^[a-zA-Z0-9]', line):
                continue
            # 提取包名+版本约束
            spec = _strip_specifier(line)
            if spec and re.match(r'^[a-zA-Z0-9][\w.\-]*', spec):
                pkgs.append(spec)
    return list(dict.fromkeys(pkgs))


def strip_hash_lines(filepath):
    """去除 requirements.txt 中的 --hash 行和 hash 值"""
    cleaned = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.strip().startswith("--hash="):
                continue
            line = re.sub(r'\s*--hash=[a-f0-9:]+\s*', '', line)
            if line.strip() and not line.strip().startswith("#"):
                cleaned.append(line)
    return cleaned


def resolve_relpath_lines(filepath, project):
    """解析相对路径引用, 返回合并后的行列表"""
    lines = []
    with open(filepath, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r ") or line.startswith("--requirement "):
                ref = line.split(None, 1)[1].strip()
                ref_path = Path(filepath).parent / ref
                if ref_path.exists():
                    with open(ref_path, encoding="utf-8", errors="ignore") as rf:
                        for rl in rf:
                            rl = rl.strip()
                            if rl and not rl.startswith("#"):
                                lines.append(rl)
            else:
                lines.append(line)
    return lines


# ═══════════════════════════════════════════════════════════════════
#  pip dry-run 执行
# ═══════════════════════════════════════════════════════════════════

def run_pip_dryrun_for_indirect(deps_with_version, project, timeout=600, batch_size=50):
    """对带版本的直接依赖运行 pip dry-run, 返回 (indirect_deps, error)
    
    indirect_deps: list of package names (is_direct=False)
    大批量依赖分批处理，合并结果
    """
    if not deps_with_version:
        return [], "无直接依赖"

    # 分批处理
    all_indirect = []
    batches = [deps_with_version[i:i+batch_size] for i in range(0, len(deps_with_version), batch_size)]
    errors = []

    for batch_idx, batch in enumerate(batches):
        tmpdir = tempfile.mkdtemp(prefix=f"indirect_gt_{batch_idx}_")
        req_file = os.path.join(tmpdir, "direct_deps.txt")
        with open(req_file, "w", encoding="utf-8") as f:
            f.write("\n".join(batch) + "\n")

        report_path = os.path.join(tmpdir, "report.json")
        try:
            result = subprocess.run(
                ["pip", "install", "--dry-run", "--quiet", "--no-cache-dir",
                 "--report", report_path, "-r", req_file],
                capture_output=True, text=True, timeout=timeout
            )
            if os.path.exists(report_path):
                with open(report_path) as f:
                    report = json.load(f)
                for item in report.get("install", []):
                    pkg = item.get("metadata", {}).get("name", "")
                    if not pkg:
                        continue
                    if not item.get("is_direct", False):
                        all_indirect.append(pkg)
            else:
                errors.append(f"batch{batch_idx}({len(batch)}deps): {result.stderr[:100]}")
        except subprocess.TimeoutExpired:
            errors.append(f"batch{batch_idx}({len(batch)}deps): 超时({timeout}s)")
        except Exception as e:
            errors.append(f"batch{batch_idx}: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    err_str = "; ".join(errors) if errors else None
    return all_indirect, err_str


# ═══════════════════════════════════════════════════════════════════
#  获取直接依赖 (带版本约束)
# ═══════════════════════════════════════════════════════════════════

def get_direct_deps_with_version(project, config_rel, category):
    """获取单个配置文件的直接依赖 (带版本约束)"""
    config_path = ANALYSIS / project / config_rel
    if not config_path.exists():
        config_path = Path(config_rel)
        if not config_path.exists():
            return []

    if category == "jax_plugins":
        if config_path.name == "pyproject.toml":
            return parse_pyproject_deps_with_version(config_path)
        else:
            return parse_setup_py_deps_with_version(config_path)

    elif category == "pyproject":
        return parse_pyproject_with_setup_fallback(config_path)

    elif category == "setup_py":
        return parse_setup_py_deps_with_version(config_path)

    elif category == "vcs_url":
        return extract_vcs_packages_with_version(config_path)

    elif category == "pkg_fail":
        return extract_packages_from_req_with_version(config_path)

    elif category in ("hash", "byod"):
        # 编译文件: 直接提取带版本的包名 (但这些包含直接+间接)
        lines = strip_hash_lines(config_path)
        pkgs = []
        for line in lines:
            spec = _strip_specifier(line)
            if spec and re.match(r'^[a-zA-Z0-9][\w.\-]*', spec):
                pkgs.append(spec)
        return pkgs

    elif category == "relpath":
        lines = resolve_relpath_lines(config_path, project)
        pkgs = []
        for line in lines:
            if line.startswith("-"):
                continue
            spec = _strip_specifier(line)
            if spec and re.match(r'^[a-zA-Z0-9][\w.\-]*', spec):
                pkgs.append(spec)
        return pkgs

    return []


def get_project_true_direct_deps(project):
    """从 DCFinder 的 direct_by_config_file.json 获取项目真实直接依赖 (非编译文件, 带版本约束)"""
    dc_path = DC_RESULTS / project / "expanded" / "direct_by_config_file.json"
    if not dc_path.exists():
        return []

    with open(dc_path) as f:
        data = json.load(f)

    deps = []
    for key, val in data.items():
        if not isinstance(val, list):
            continue
        # 排除编译文件
        is_compiled = any(kw in key.lower() for kw in ["compiled", "byod", "requirements-doc"])
        if is_compiled:
            continue
        for d in val:
            if isinstance(d, dict) and d.get("name"):
                name = d["name"]
                spec = d.get("specifier", "")
                if spec:
                    deps.append(f"{name}{spec}")
                else:
                    deps.append(name)

    return list(dict.fromkeys(deps))


# ═══════════════════════════════════════════════════════════════════
#  主处理函数
# ═══════════════════════════════════════════════════════════════════

def process_non_compiled(project, config_rel, rname, category):
    """处理非编译类别: 获取直接依赖 → pip dry-run → 间接依赖"""
    deps = get_direct_deps_with_version(project, config_rel, category)
    if not deps:
        return (project, config_rel, rname, category, [], "无直接依赖")

    indirect, err = run_pip_dryrun_for_indirect(deps, project)
    method = f"pip dry-run on {len(deps)} direct deps"
    if err:
        method += f" ({err})"
    return (project, config_rel, rname, category, indirect, method)


def process_compiled(project):
    """处理编译类别: 获取项目真实直接依赖 → pip dry-run → 间接依赖"""
    true_deps = get_project_true_direct_deps(project)

    # 也加入非编译类别的补充GT直接依赖
    for proj, config_rel, rname, cat in MISSING_FILES:
        if proj == project and cat in NON_COMPILED_CATS:
            extra = get_direct_deps_with_version(proj, config_rel, cat)
            for d in extra:
                if d not in true_deps:
                    true_deps.append(d)

    if not true_deps:
        return [], "无真实直接依赖"

    indirect, err = run_pip_dryrun_for_indirect(true_deps, project, timeout=600, batch_size=40)
    method = f"pip dry-run on {len(true_deps)} true direct deps"
    if err:
        method += f" ({err})"
    return indirect, method


def main():
    print("=" * 80)
    print("补充间接依赖 GT — 对直接依赖运行 pip dry-run 获取传递依赖")
    print("=" * 80)

    # 读取现有 supplementary_gt.csv
    supp_csv = RESULTS / "supplementary_gt.csv"
    existing_rows = []
    with open(supp_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            existing_rows.append(row)

    print(f"现有 supplementary_gt.csv: {len(existing_rows)} 行 (全部 is_direct=True)")

    # 收集需要处理的项目
    projects_with_compiled = set()
    for proj, _, _, cat in MISSING_FILES:
        if cat in COMPILED_CATS:
            projects_with_compiled.add(proj)

    results = []

    # A. 处理非编译类别 (每文件独立 pip dry-run)
    print("\n--- A. 非编译类别 (per-file pip dry-run) ---")
    non_compiled_tasks = [(p, c, r, cat) for p, c, r, cat in MISSING_FILES if cat in NON_COMPILED_CATS]

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(process_non_compiled, p, c, r, cat): (p, c, r, cat)
            for p, c, r, cat in non_compiled_tasks
        }
        for i, future in enumerate(as_completed(futures)):
            p, c, r, cat = futures[future]
            result = future.result()
            project, config_rel, rname, category, indirect, method = result
            print(f"  [{i+1}/{len(non_compiled_tasks)}] {project:30s} {category:12s} → {len(indirect):3d} 间接依赖 [{method}]")
            results.append(result)

    # B. 处理编译类别 (per-project pip dry-run)
    print("\n--- B. 编译类别 (per-project pip dry-run on true direct deps) ---")
    project_indirect_cache = {}
    for project in sorted(projects_with_compiled):
        indirect, method = process_compiled(project)
        project_indirect_cache[project] = (indirect, method)
        print(f"  {project:30s} → {len(indirect):3d} 间接依赖 [{method}]")

        # 分配给该项目的所有编译配置文件
        for proj, config_rel, rname, cat in MISSING_FILES:
            if proj == project and cat in COMPILED_CATS:
                results.append((project, config_rel, rname, cat, indirect, method))

    # 写入间接依赖到 supplementary_gt.csv
    print("\n" + "=" * 80)
    print("追加间接依赖到 supplementary_gt.csv")
    print("=" * 80)

    new_rows = []
    for project, config_rel, rname, category, indirect, method in results:
        for pkg in indirect:
            new_rows.append({
                "project": project,
                "config_file": rname,
                "category": category,
                "package": pkg,
                "is_direct": "False",
                "method": f"间接依赖 ({method})",
            })

    # 追加到 CSV
    with open(supp_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        for row in new_rows:
            writer.writerow(row)

    print(f"追加 {len(new_rows)} 行间接依赖 (is_direct=False)")
    print(f"supplementary_gt.csv 总行数: {len(existing_rows) + len(new_rows)}")

    # 按项目汇总
    print("\n=== 按项目汇总 ===")
    by_proj = {}
    for project, config_rel, rname, category, indirect, method in results:
        if project not in by_proj:
            by_proj[project] = {"indirect": set(), "files": 0}
        by_proj[project]["indirect"].update(p.lower() for p in indirect)
        by_proj[project]["files"] += 1

    for proj in sorted(by_proj.keys()):
        info = by_proj[proj]
        print(f"  {proj:30s}: {info['files']:2d} files, {len(info['indirect']):3d} unique indirect deps")


if __name__ == "__main__":
    main()
