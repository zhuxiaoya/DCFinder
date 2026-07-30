#!/usr/bin/env python3
"""
supplement_pip_gt.py — 为 pip dry-run 失败的 29 个配置文件生成补充 GT (Ground Truth)

分类处理:
  A. hash 锁定文件 (12): 去除 --hash 后重新 pip dry-run
  B. byod 文件 (2): 检查是否含 hash, 如有则同 A 处理
  C. 相对路径文件 (2): 解析路径合并后重新 pip dry-run
  D. pyproject.toml (5): 解析 [project.dependencies] / [tool.poetry.dependencies]
  E. setup.py (1): AST 解析 install_requires
  F. jax_plugins (4): 标记为无直接依赖
  G. VCS URL (1): 提取包名
  H. 包解析失败 (2): 从文件直接提取包名
"""

import os, re, csv, json, ast, subprocess, tempfile, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 路径常量 ──
BASE = Path(__file__).resolve().parent
ANALYSIS = BASE / "analysis_program"
RESULTS = BASE / "dynamic_results_all_dryrun_v4"
DC_RESULTS = BASE / "comparison_results_v4"

# ── 29 个缺失文件清单 (project, config_path, expected_rname, category) ──
MISSING_FILES = [
    # F. jax_plugins (4) — 构建时文件缺失, 无直接依赖
    ("jax-main", "jax_plugins/cuda/setup.py", "setup_py__jax_plugins__cuda__setup.py.json", "jax_plugins"),
    ("jax-main", "jax_plugins/cuda/pyproject.toml", "pyproject__jax_plugins__cuda__pyproject.toml.json", "jax_plugins"),
    ("jax-main", "jax_plugins/rocm/setup.py", "setup_py__jax_plugins__rocm__setup.py.json", "jax_plugins"),
    ("jax-main", "jax_plugins/rocm/pyproject.toml", "pyproject__jax_plugins__rocm__pyproject.toml.json", "jax_plugins"),
    # D. pyproject.toml — setuptools-scm 失败 (5)
    ("ipex-llm-main", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("ray-project_ray", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("vllm-main", "pyproject.toml", "pyproject__pyproject.toml.json", "pyproject"),
    ("vllm-main", "benchmarks/pyproject.toml", "pyproject__benchmarks__pyproject.toml.json", "pyproject"),
    ("vllm-main", "examples/pyproject.toml", "pyproject__examples__pyproject.toml.json", "pyproject"),
    # E. setup.py — install_requires 格式错误 (1)
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
    # B. byod 文件 (2) — 可能含 hash
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


def strip_hash_lines(filepath):
    """去除 requirements.txt 中的 --hash 行和 hash 值"""
    cleaned = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            # 跳过纯 --hash 行
            if line.strip().startswith("--hash="):
                continue
            # 去除行尾的 --hash=... 部分
            line = re.sub(r'\s*--hash=[a-f0-9:]+\s*', '', line)
            # 跳过空行和注释
            if line.strip() and not line.strip().startswith("#"):
                cleaned.append(line)
    return cleaned


def resolve_relpath(filepath, project):
    """解析相对路径引用，返回合并后的包列表"""
    proj_dir = ANALYSIS / project
    file_dir = Path(filepath).parent
    packages = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 检查是否是相对路径引用
            if line.startswith("../") or line.startswith("./") or (
                not line.startswith("-") and "/" in line and not line.startswith("git+")
                and not line.startswith("http") and line.endswith(".txt")
            ):
                # 解析相对路径
                resolved = (file_dir / line).resolve()
                if resolved.exists():
                    # 递归读取引用的文件
                    with open(resolved, encoding="utf-8") as rf:
                        for rline in rf:
                            rline = rline.strip()
                            if rline and not rline.startswith("#") and not rline.startswith("-"):
                                packages.append(rline)
                else:
                    print(f"  ⚠ 相对路径无法解析: {line} → {resolved}")
            else:
                packages.append(line)
    return packages


def parse_pyproject_deps(filepath):
    """解析 pyproject.toml 的 [project.dependencies] 和 [tool.poetry.dependencies]"""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib

    with open(filepath, "rb") as f:
        data = tomllib.load(f)

    deps = []

    # [project.dependencies]
    project = data.get("project", {})
    if "dependencies" in project:
        deps.extend(project["dependencies"])

    # [project.optional-dependencies]
    for group, items in project.get("optional-dependencies", {}).items():
        deps.extend(items)

    # [tool.poetry.dependencies] (排除 "python" key)
    poetry = data.get("tool", {}).get("poetry", {})
    for key, val in poetry.get("dependencies", {}).items():
        if key.lower() == "python":
            continue
        if isinstance(val, str):
            deps.append(f"{key}{val}" if val.startswith((">","<","=","!","~")) else f"{key}=={val}")
        elif isinstance(val, dict):
            version = val.get("version", "")
            if version:
                deps.append(f"{key}{version}" if version.startswith((">","<","=","!","~")) else f"{key}=={version}")
            else:
                deps.append(key)
        else:
            deps.append(key)

    # [tool.poetry.group.*.dependencies]
    for group, gdata in poetry.get("group", {}).items():
        for key, val in gdata.get("dependencies", {}).items():
            if key.lower() == "python":
                continue
            if isinstance(val, str):
                deps.append(f"{key}{val}" if val.startswith((">","<","=","!","~")) else f"{key}=={val}")
            else:
                deps.append(key)

    # 注意: [build-system].requires 是构建依赖, 不是运行时直接依赖
    # 不提取 build-system.requires 作为直接依赖

    # 去重
    seen = set()
    unique = []
    for d in deps:
        name = re.split(r'[<>=!~;\[]', d)[0].strip().lower()
        if name and name not in seen:
            seen.add(name)
            unique.append(d)

    return unique


def parse_setup_py_deps(filepath):
    """AST 解析 setup.py 的 install_requires"""
    with open(filepath, encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)

    install_requires = []

    for node in ast.walk(tree):
        # 查找 setup(install_requires=...) 调用
        if isinstance(node, ast.Call):
            func = node.func
            # setup(...) 或 setuptools.setup(...)
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            if func_name in ("setup",):
                for kw in node.keywords:
                    if kw.arg == "install_requires":
                        val = _ast_node_to_value(kw.value)
                        if isinstance(val, list):
                            install_requires.extend(val)
                        elif isinstance(val, str):
                            install_requires.append(val)
                    elif kw.arg == "setup_requires":
                        val = _ast_node_to_value(kw.value)
                        if isinstance(val, list):
                            install_requires.extend(val)
                        elif isinstance(val, str):
                            install_requires.append(val)
                    elif kw.arg == "extras_require":
                        val = _ast_node_to_value(kw.value)
                        if isinstance(val, dict):
                            for items in val.values():
                                if isinstance(items, list):
                                    install_requires.extend(items)

    # 如果 AST 解析失败（如 setup.py 动态读取 requirements.txt），
    # 尝试读取引用的 requirements.txt
    if not install_requires:
        # 检查 setup.py 是否读取 requirements.txt
        if "requirements.txt" in source:
            proj_dir = Path(filepath).parent
            req_path = proj_dir / "requirements.txt"
            if req_path.exists():
                with open(req_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith("-"):
                            install_requires.append(line)

    # 去重并过滤 pip 选项
    seen = set()
    unique = []
    for d in install_requires:
        d = d.strip()
        if not d or d.startswith("-"):
            continue
        name = re.split(r'[<>=!~;\[]', d)[0].strip().lower()
        if name and name not in seen:
            seen.add(name)
            unique.append(d)

    return unique


def _ast_node_to_value(node):
    """将 AST 节点转换为 Python 值"""
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Str):  # Python < 3.8
        return node.s
    elif isinstance(node, ast.List):
        return [_ast_node_to_value(e) for e in node.elts]
    elif isinstance(node, ast.Tuple):
        return tuple(_ast_node_to_value(e) for e in node.elts)
    elif isinstance(node, ast.Name):
        # 尝试从源码中查找变量定义
        return node.id  # 返回变量名作为 fallback
    elif isinstance(node, ast.JoinedStr):
        # f-string, 尝试拼接
        parts = []
        for val in node.values:
            if isinstance(val, ast.Constant):
                parts.append(str(val.value))
            elif isinstance(val, ast.FormattedValue):
                parts.append("{}")
        return "".join(parts)
    return None


def extract_vcs_packages(filepath):
    """从含 VCS URL 的 requirements.txt 提取包名"""
    packages = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # git+https://github.com/xxx/yyy.git → yyy
            m = re.match(r'([a-zA-Z0-9_.-]+)\s*@\s*git\+', line)
            if m:
                packages.append(m.group(1))
                continue
            # git+https://github.com/xxx/yyy.git (无包名前缀)
            m = re.search(r'/([^/]+)\.git', line)
            if m:
                packages.append(m.group(1).lower().replace("_", "-"))
                continue
            # 普通包
            if not line.startswith("-") and not line.startswith("git+") and not line.startswith("http"):
                name = re.split(r'[<>=!~;\[]', line)[0].strip()
                if name:
                    packages.append(name)
    return packages


def extract_packages_from_req(filepath):
    """从 requirements.txt 直接提取包名（不解析依赖树）"""
    packages = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 跳过 pip 选项
            if line.startswith("-"):
                continue
            # 跳过相对路径引用
            if line.startswith("../") or line.startswith("./"):
                continue
            # 跳过 VCS URL
            if "git+" in line or "svn+" in line or "hg+" in line or "bzr+" in line:
                continue
            # 提取包名
            name = re.split(r'[<>=!~;\[ @]', line)[0].strip()
            if name and name.lower() not in ("-r", "-e"):
                packages.append(name)
    return list(dict.fromkeys(packages))  # 去重保序


def run_pip_dryrun_cleaned(filepath, project, rname, cleaned_lines, label="hash_stripped"):
    """对清洗后的 requirements 重新运行 pip dry-run"""
    if not cleaned_lines:
        return [], f"{label}: 清洗后无有效行"

    # 写入临时文件
    tmpdir = tempfile.mkdtemp(prefix="supp_gt_")
    tmpfile = os.path.join(tmpdir, os.path.basename(filepath))
    with open(tmpfile, "w", encoding="utf-8") as f:
        f.write("\n".join(cleaned_lines) + "\n")

    report_path = os.path.join(tmpdir, "report.json")
    try:
        result = subprocess.run(
            ["pip", "install", "--dry-run", "--quiet", "--no-cache-dir",
             "--report", report_path, "-r", tmpfile],
            capture_output=True, text=True, timeout=120
        )
        if os.path.exists(report_path):
            with open(report_path) as f:
                report = json.load(f)
            packages = []
            for item in report.get("install", []):
                pkg = item.get("metadata", {}).get("name", "")
                if pkg:
                    is_direct = item.get("is_direct", False)
                    requested = item.get("requested", False) or is_direct
                    packages.append((pkg, requested))
            return packages, None
        else:
            # pip dry-run 失败，手动提取包名
            packages = extract_packages_from_req(tmpfile)
            return [(p, True) for p in packages], f"{label}: pip dry-run 失败, 手动提取"
    except Exception as e:
        return [], f"{label}: 异常 {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_one(project, config_rel, rname, category):
    """处理单个失败文件，返回 (project, config_rel, rname, category, packages, method)"""
    config_path = ANALYSIS / project / config_rel
    if not config_path.exists():
        # 尝试绝对路径
        config_path = Path(config_rel)
        if not config_path.exists():
            return (project, config_rel, rname, category, [], "文件不存在")

    packages = []
    method = ""

    if category == "jax_plugins":
        # F. jax_plugins — 检查是否有依赖
        if config_path.name == "pyproject.toml":
            deps = parse_pyproject_deps(config_path)
            packages = [(d.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip(), True) for d in deps]
        else:
            deps = parse_setup_py_deps(config_path)
            packages = [(d.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip(), True) for d in deps]
        method = "静态解析 (jax_plugins)"
        if not packages:
            method = "无直接依赖 (jax_plugins 空配置)"

    elif category == "pyproject":
        # D. pyproject.toml — 解析 [project.dependencies]
        deps = parse_pyproject_deps(config_path)
        packages = [(d.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip(), True) for d in deps]
        method = "tomllib 解析 [project.dependencies]"

    elif category == "setup_py":
        # E. setup.py — AST 解析 install_requires
        deps = parse_setup_py_deps(config_path)
        packages = [(d.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip(), True) for d in deps]
        method = "AST 解析 install_requires"

    elif category == "vcs_url":
        # G. VCS URL
        pkgs = extract_vcs_packages(config_path)
        packages = [(p, True) for p in pkgs]
        method = "VCS URL 包名提取"

    elif category == "pkg_fail":
        # H. 包解析失败 — 直接提取包名
        pkgs = extract_packages_from_req(config_path)
        packages = [(p, True) for p in pkgs]
        method = "requirements.txt 直接提取"

    elif category == "relpath":
        # C. 相对路径引用 — 解析路径合并
        merged = resolve_relpath(config_path, project)
        if merged:
            # 尝试 pip dry-run
            cleaned = [l for l in merged if not l.startswith("-")]
            if cleaned:
                pkgs = list(dict.fromkeys(
                    re.split(r'[<>=!~;\[]', l)[0].strip()
                    for l in cleaned if l.strip()
                ))
                packages = [(p, True) for p in pkgs if p]
            method = "相对路径解析 + 包名提取"
        else:
            method = "相对路径解析失败"

    elif category in ("hash", "byod"):
        # A/B. hash 锁定文件 — 去 hash 后重新 pip dry-run
        cleaned_lines = strip_hash_lines(config_path)
        has_hash = any("--hash" in line for line in open(config_path, encoding="utf-8"))

        if has_hash or category == "hash":
            # 先去 hash, 再尝试 pip dry-run
            pkgs, err = run_pip_dryrun_cleaned(config_path, project, rname, cleaned_lines, "hash_stripped")
            if pkgs:
                packages = pkgs
                method = "去 hash + pip dry-run"
            else:
                # pip dry-run 仍然失败, 手动提取包名
                pkgs = extract_packages_from_req(config_path)
                # 过滤掉 hash 行
                pkgs = [p for p in pkgs if not p.startswith("--")]
                packages = [(p, True) for p in pkgs]
                method = f"去 hash + 手动提取 (pip dry-run 失败: {err})"
        else:
            # byod 文件无 hash, 直接提取包名
            pkgs = extract_packages_from_req(config_path)
            packages = [(p, True) for p in pkgs]
            method = "直接提取包名 (byod 无 hash)"

    return (project, config_rel, rname, category, packages, method)


def main():
    print("=" * 80)
    print("补充 GT 生成 — 为 29 个 pip dry-run 失败的配置文件提取直接依赖")
    print("=" * 80)

    results = []

    # 并行处理
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(process_one, proj, path, rname, cat): (proj, path, rname, cat)
            for proj, path, rname, cat in MISSING_FILES
        }
        for i, future in enumerate(as_completed(futures)):
            proj, path, rname, cat = futures[future]
            result = future.result()
            project, config_rel, rname, category, packages, method = result
            n = len(packages)
            direct = sum(1 for _, d in packages if d)
            print(f"[{i+1}/{len(MISSING_FILES)}] {project:30s} {cat:12s} → {n:3d} packages ({direct} direct) [{method}]")
            results.append(result)

    # 写入 CSV
    output_csv = RESULTS / "supplementary_gt.csv"
    fieldnames = ["project", "config_file", "category", "package", "is_direct", "method"]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for project, config_rel, rname, category, packages, method in results:
            for pkg, is_direct in packages:
                w.writerow({
                    "project": project,
                    "config_file": rname,
                    "category": category,
                    "package": pkg,
                    "is_direct": is_direct,
                    "method": method,
                })

    print()
    print("=" * 80)
    print(f"输出: {output_csv}")
    total_pkgs = sum(len(packages) for _, _, _, _, packages, _ in results)
    total_direct = sum(sum(1 for _, d in packages if d) for _, _, _, _, packages, _ in results)
    print(f"总包数: {total_pkgs}, 直接依赖: {total_direct}")

    # 按项目汇总
    print()
    print("=== 按项目汇总 ===")
    by_proj = {}
    for project, config_rel, rname, category, packages, method in results:
        if project not in by_proj:
            by_proj[project] = {"files": 0, "packages": set(), "methods": set()}
        by_proj[project]["files"] += 1
        for pkg, _ in packages:
            by_proj[project]["packages"].add(pkg.lower())
        by_proj[project]["methods"].add(method)

    for proj in sorted(by_proj.keys()):
        info = by_proj[proj]
        print(f"  {proj:30s}: {info['files']:2d} files, {len(info['packages']):3d} unique direct deps")
        for m in info["methods"]:
            print(f"    → {m}")

    # 与 DCFinder 对比 — 使用完整路径匹配
    print()
    print("=== 与 DCFinder 对比 ===")
    for project, config_rel, rname, category, packages, method in results:
        dc_path = DC_RESULTS / project / "expanded" / "direct_by_config_file.json"
        if not dc_path.exists():
            continue
        with open(dc_path) as f:
            dc_data = json.load(f)

        # 使用 config_rel 构建匹配模式: 项目目录下的相对路径
        # DCFinder key 可能是绝对路径或相对路径，都包含 config_rel 部分
        config_key = None
        for key in dc_data:
            if config_rel in key:
                config_key = key
                break
        if not config_key:
            # DCFinder 没有扫描此文件
            supp_deps = set(p.lower() for p, _ in packages if _)
            if supp_deps:
                print(f"  [{project}] {config_rel}")
                print(f"    DCFinder: 0 (未扫描), 补充GT: {len(supp_deps)}")
            continue

        # DCFinder value 是 list of {'name': str, 'specifier': str}
        dc_val = dc_data[config_key]
        if isinstance(dc_val, list):
            dc_deps = set(d.get("name", "").lower() for d in dc_val if isinstance(d, dict))
        elif isinstance(dc_val, dict):
            dc_deps = set(d.get("name", "").lower() for d in dc_val.get("dependencies", []))
        else:
            continue

        supp_deps = set(p.lower() for p, _ in packages if _)
        common = dc_deps & supp_deps
        dc_only = dc_deps - supp_deps
        supp_only = supp_deps - dc_deps
        if dc_only or supp_only:
            print(f"  [{project}] {config_rel}")
            print(f"    DCFinder: {len(dc_deps)}, 补充GT: {len(supp_deps)}, 共同: {len(common)}")
            if dc_only:
                print(f"    DCFinder 独有: {sorted(dc_only)[:8]}")
            if supp_only:
                print(f"    补充GT 独有: {sorted(supp_only)[:8]}")
        else:
            print(f"  [{project}] {config_rel} — 完全一致 ({len(common)} deps)")


if __name__ == "__main__":
    main()
