import os
import fnmatch
import csv
import re
import tomllib  # Python 3.11+ 内置，若低于3.11请 pip install tomli 并改 import

# 配置文件模式（保持不变）
dependencies_patterns = {
    'requirement': (
        '*requirement*.txt',
        '*requirement*.pip',
        '*requirement*.in',
        '*requires.txt',
        '*requirements/*.txt',
        '*requirements/*.pip',
        '*requirements/*.in',
    ),
    'pyproject': ('*pyproject.toml',),
    'setup_py': ('*setup.py',),
    'setup_cfg': ('*setup.cfg',),
    'Pipfile': ('*Pipfile',)
}


# ================== 核心改动 1：递归查找所有依赖配置文件 ==================
def find_all_dependency_files(directory):
    """
    递归遍历目录，返回一个字典：
    {
        'requirement': [path1, path2, ...],
        'pyproject': [path3, ...],
        ...
    }
    """
    matches = {category: [] for category in dependencies_patterns}

    # 将所有模式展开成一个列表，方便快速匹配
    all_patterns = []
    for patterns in dependencies_patterns.values():
        all_patterns.extend(patterns)

    for root, dirs, files in os.walk(directory):
        # 跳过隐藏目录（如 .git, .venv, __pycache__）以提升速度
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for filename in files:
            file_path = os.path.join(root, filename)
            # 计算相对于项目根目录的路径，用于模式匹配（如 requirements/dev.txt）
            rel_path = os.path.relpath(file_path, directory)

            # 检查是否匹配任意一种模式
            for category, patterns in dependencies_patterns.items():
                for pattern in patterns:
                    if fnmatch.fnmatch(rel_path, pattern):
                        matches[category].append(file_path)
                        break  # 匹配到一个类别即可，避免重复添加
                else:
                    continue
                break
    return matches


# ================== 核心改动 2：只提取指定文件列表中的 URL ==================
def extract_urls_from_files(file_paths):
    """
    接收一个文件路径列表，仅从这些文件中提取 --index-url / -i 等 URL
    """
    url_pattern = re.compile(
        r'(?:--index-url|-i|--extra-index-url)\s*[= ]\s*(https?://[^\s\'"]+)',
        re.IGNORECASE
    )
    urls = set()

    for file_path in file_paths:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                matches = url_pattern.findall(content)
                for url in matches:
                    if 'pypi.org' not in url and 'pypi.python.org' not in url:
                        urls.add(url)

                # 【额外增强】如果是 pyproject.toml，顺便解析 poetry/pipenv 源
                if file_path.endswith('pyproject.toml'):
                    try:
                        data = tomllib.loads(content)
                        sources = data.get('tool', {}).get('poetry', {}).get('source', [])
                        for src in sources:
                            if 'url' in src and 'pypi.org' not in src['url']:
                                urls.add(src['url'])
                        # pipenv 源
                        sources_pipenv = data.get('source', [])
                        for src in sources_pipenv:
                            if 'url' in src and 'pypi.org' not in src['url']:
                                urls.add(src['url'])
                    except Exception:
                        pass  # 解析失败忽略
        except Exception:
            continue  # 读取失败忽略
    return urls


# ================== 重构后的主函数 ==================
def analyze_projects(directory):
    category_counts = {category: 0 for category in dependencies_patterns}
    no_config_projects = []
    all_projects = set()
    non_pypi_url_map = {}

    i = 0
    items = os.scandir(directory)
    for item in items:
        if item.is_dir():
            project_name = item.name
            project_path = item.path
            all_projects.add(project_name)
            i += 1
            print(f"正在分析第 {i} 个项目：{project_name}")

            # 1. 递归找到所有真正的依赖配置文件
            matched_files_dict = find_all_dependency_files(project_path)

            # 统计哪些类别有文件
            found_categories = [cat for cat, files in matched_files_dict.items() if files]

            if found_categories:
                for category in found_categories:
                    category_counts[category] += 1
            else:
                no_config_projects.append(project_name)

            # 2. 【关键改动】仅针对这些匹配到的配置文件提取 URL
            all_matched_file_paths = []
            for files in matched_files_dict.values():
                all_matched_file_paths.extend(files)

            if all_matched_file_paths:
                urls = extract_urls_from_files(all_matched_file_paths)
                if urls:
                    non_pypi_url_map[project_name] = urls

    # ========== 输出部分（保持您原有的完全不变） ==========
    output_file = 'project_analysis1.csv'
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Category', 'Project Count'])
        for category, count in category_counts.items():
            writer.writerow([category, count])
        writer.writerow([])
        writer.writerow(['Projects without any config files', 'Count'])
        writer.writerow(['Total', len(no_config_projects)])
        for project in no_config_projects:
            writer.writerow([project, ''])

    print(f"配置文件统计完成，结果写入 {output_file}")

    output_url_file = 'non_pypi_urls.txt'
    with open(output_url_file, 'w', encoding='utf-8') as f:
        f.write("项目：使用的非PyPI的各个url\n")
        f.write("=" * 50 + "\n")
        for project_name, urls in sorted(non_pypi_url_map.items()):
            url_list_str = ", ".join(sorted(urls))
            f.write(f"{project_name}：{url_list_str}\n")

    print(f"非 PyPI URL 扫描完成，结果写入 {output_url_file}")
    print(f"共发现 {len(non_pypi_url_map)} 个项目使用了非 PyPI 的包索引 URL。")

    if non_pypi_url_map:
        print("\n--- 非 PyPI URL 项目清单 ---")
        for project_name, urls in sorted(non_pypi_url_map.items()):
            print(f"{project_name}：{', '.join(sorted(urls))}")
    else:
        print("未发现任何项目使用非 PyPI 的包索引 URL。")


if __name__ == "__main__":
    directory_path = r'/home/lab314/zxy/dependncy_confusion/analysis_program'
    analyze_projects(directory_path)