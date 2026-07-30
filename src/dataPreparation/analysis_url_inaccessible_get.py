import requests
import sys
import re
import time
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import warnings
from urllib3.exceptions import InsecureRequestWarning

# 忽略 SSL 警告（调试内网时建议开启）
warnings.simplefilter('ignore', InsecureRequestWarning)

# ---------- 解析 non_pypi_urls.txt ----------
def parse_non_pypi_file(file_path):
    """
    解析 non_pypi_urls.txt，返回 {项目名: [url列表]}
    """
    projects = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 跳过标题行或分隔线
                if line.startswith('项目：') or line.startswith('===') or line.startswith('项目:'):
                    continue
                # 格式：项目名：url1, url2, ...
                if '：' in line:
                    parts = line.split('：', 1)
                elif ':' in line:
                    parts = line.split(':', 1)
                else:
                    continue
                project_name = parts[0].strip()
                url_part = parts[1].strip()
                if url_part:
                    # 分割多个URL（用逗号或中文逗号分隔）
                    urls = re.split(r'[,，]\s*', url_part)
                    urls = [u.strip() for u in urls if u.strip()]
                    projects[project_name] = urls
    except FileNotFoundError:
        print(f"错误：文件 {file_path} 不存在")
        sys.exit(1)
    return projects

# ---------- 增强的可达性检测 ----------
def check_url_accessible(url, timeout=10, retries=2):
    """
    更可靠的 URL 可达性检测：
    1. 先尝试 HEAD（快速）
    2. 若失败（状态码非 2xx/3xx 或异常），改用 GET 仅获取响应头（stream=True）
    3. 跳过 SSL 验证，使用 pip 的 User-Agent
    4. 内置重试机制（连接错误或超时自动重试）
    """
    headers = {
        'User-Agent': 'pip/23.1.2 (Python) requests/2.31.0'
    }
    session = requests.Session()
    # 设置重试策略（仅对连接错误和超时重试）
    retry_strategy = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],  # 服务端错误也重试
        allowed_methods=["HEAD", "GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        # 第一次尝试：HEAD
        resp = session.head(url, timeout=timeout, allow_redirects=True,
                            verify=False, headers=headers)
        if resp.status_code < 400:
            return True

        # 如果 HEAD 返回 405/403/404，改用 GET（只读头部，不下载内容）
        if resp.status_code in (405, 403, 404):
            resp_get = session.get(url, timeout=timeout, stream=True,
                                   verify=False, headers=headers)
            # 只要状态码正常，即为可访问
            if resp_get.status_code < 400:
                return True
            # 即使状态码非 2xx，也可能是因为需要认证，但服务器是活的
            # 这里我们只根据状态码判断，你也可以进一步检查内容
            return False
        return False

    except (requests.exceptions.SSLError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError) as e:
        # 这些异常会在重试策略中自动处理，若重试后仍然失败，则返回 False
        return False
    except Exception:
        return False
    finally:
        session.close()

# ---------- 主函数 ----------
def main():
    input_file = 'non_pypi_urls.txt'
    projects = parse_non_pypi_file(input_file)
    total = len(projects)
    accessible_projects = []
    excluded_projects = {}  # 项目名 -> 不可访问的URL列表

    print(f"共发现 {total} 个项目，开始检查 URL 可达性...\n")
    for idx, (project, urls) in enumerate(projects.items(), 1):
        print(f"[{idx}/{total}] 检查项目：{project}")
        all_ok = True
        bad_urls = []
        for url in urls:
            ok = check_url_accessible(url)
            if not ok:
                all_ok = False
                bad_urls.append(url)
                print(f"  ❌ 不可访问：{url}")
            else:
                print(f"  ✅ 可访问：{url}")
        if all_ok:
            accessible_projects.append(project)
        else:
            excluded_projects[project] = bad_urls
        print()

    # 统计
    excluded_count = len(excluded_projects)
    accessible_count = len(accessible_projects)

    print("=" * 50)
    print(f"总项目数：{total}")
    print(f"可访问项目（所有URL均可达）：{accessible_count}")
    print(f"被排除项目（至少一个URL不可访问）：{excluded_count}")

    if excluded_projects:
        print("\n--- 被排除的项目详情 ---")
        for proj, bads in excluded_projects.items():
            print(f"{proj}：{', '.join(bads)}")

    # 将结果写入文件
    with open('url_access_summary.txt', 'w', encoding='utf-8') as f:
        f.write(f"总项目数：{total}\n")
        f.write(f"可访问项目数：{accessible_count}\n")
        f.write(f"被排除项目数：{excluded_count}\n\n")
        if excluded_projects:
            f.write("被排除项目（不可访问的URL）：\n")
            for proj, bads in excluded_projects.items():
                f.write(f"{proj}：{', '.join(bads)}\n")

    print(f"\n详细结果已保存到 url_access_summary.txt")

if __name__ == "__main__":
    # 如果未安装 requests，请先运行 pip install requests
    main()
