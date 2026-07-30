import os, re, csv
from packaging.utils import canonicalize_name

ANALYSIS_BASE = '/home/lab314/zxy/dependncy_confusion/analysis_program'
DCFINDER_BASE = '/home/lab314/zxy/dependncy_confusion/comparison_results_v4'
DRYRUN_BASE = '/home/lab314/zxy/dependncy_confusion/dynamic_results_all_dryrun_v4'


def parse_requirements_simple(filepath):
    """模拟 confused 工具的简单 requirements.txt 解析：只读根目录 requirements*.txt"""
    pkgs = set()
    if not os.path.exists(filepath):
        return pkgs
    with open(filepath, encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 跳过选项行（但 confused 工具实际会把 --extra-index-url 当包名，这里做合理处理）
            if line.startswith('--') or line.startswith('-r ') or line.startswith('-c '):
                continue
            # 跳过 VCS URL
            if any(line.startswith(p) for p in ('git+', 'svn+', 'hg+', 'bzr+')):
                continue
            if re.match(r'^https?://', line):
                continue
            # 提取包名
            pkg_clean = re.sub(r'\[.*?\]', '', line)  # 去掉 extras
            pkg_line = pkg_clean.split(';')[0].strip()  # 去掉环境标记
            pkg_name = None
            for op in ['>=', '<=', '==', '!=', '~=', '>', '<']:
                if op in pkg_line:
                    pkg_name = pkg_line.split(op)[0].strip()
                    break
            if pkg_name is None:
                pkg_name = pkg_line.strip()
            # 去掉包名前的 -e 等前缀
            pkg_name = re.sub(r'^-e\s+', '', pkg_name)
            pkg_name = pkg_name.lstrip('- ')
            if pkg_name and re.match(r'^[a-zA-Z0-9][\w.\-]*$', pkg_name):
                pkgs.add(canonicalize_name(pkg_name))
    return pkgs


def get_confused_direct_deps(project):
    """模拟 confused 工具：只读根目录 requirements*.txt，不读 setup.py/pyproject.toml/子目录"""
    proj_dir = os.path.join(ANALYSIS_BASE, project)
    pkgs = set()
    if not os.path.isdir(proj_dir):
        return pkgs
    for fname in os.listdir(proj_dir):
        fpath = os.path.join(proj_dir, fname)
        if not os.path.isfile(fpath):
            continue
        lower = fname.lower()
        if lower.startswith('requirements') and lower.endswith(('.txt', '.pip', '.in')):
            pkgs |= parse_requirements_simple(fpath)
    return pkgs


def get_dcfinder_direct_deps(project):
    """DCFinder 直接依赖"""
    f = os.path.join(DCFINDER_BASE, project, 'expanded', 'direct_dependency_packages.txt')
    pkgs = set()
    if os.path.exists(f):
        with open(f) as fh:
            for line in fh:
                name = re.split(r'[<>=!~\[]', line.strip())[0].strip()
                if name:
                    pkgs.add(canonicalize_name(name))
    return pkgs


def get_pip_direct_deps(project):
    """pip 直接依赖（ground truth）= pip dry-run 结果 + 补充 GT（针对 pip 失败的 29 个文件）"""
    pkgs = set()
    # 1. pip dry-run 实际安装结果
    with open(os.path.join(DRYRUN_BASE, 'all_deps_detail.csv')) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('project') == project and row.get('is_direct', '').lower() == 'true':
                pkg = row.get('package', '').strip()
                if pkg:
                    pkgs.add(canonicalize_name(pkg))
    # 2. 补充 GT（pip dry-run 失败文件的静态解析结果）
    supp_path = os.path.join(DRYRUN_BASE, 'supplementary_gt.csv')
    if os.path.exists(supp_path):
        with open(supp_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('project') == project and row.get('is_direct', '').lower() == 'true':
                    pkg = row.get('package', '').strip()
                    if pkg:
                        pkgs.add(canonicalize_name(pkg))
    return pkgs


def main():
    # 获取所有项目列表
    projects = sorted(os.listdir(DCFINDER_BASE))

    results = []
    for proj in projects:
        confused = get_confused_direct_deps(proj)
        dcfinder = get_dcfinder_direct_deps(proj)
        pip_gt = get_pip_direct_deps(proj)

        if not pip_gt:
            continue  # 跳过没有 pip GT 的项目

        confused_same = confused & pip_gt
        dcfinder_same = dcfinder & pip_gt

        confused_prec = len(confused_same) / len(confused) * 100 if confused else 0
        confused_rec = len(confused_same) / len(pip_gt) * 100 if pip_gt else 0
        dcfinder_prec = len(dcfinder_same) / len(dcfinder) * 100 if dcfinder else 0
        dcfinder_rec = len(dcfinder_same) / len(pip_gt) * 100 if pip_gt else 0

        results.append({
            'project': proj,
            'confused_total': len(confused),
            'dcfinder_total': len(dcfinder),
            'pip_gt': len(pip_gt),
            'confused_same': len(confused_same),
            'dcfinder_same': len(dcfinder_same),
            'confused_prec': confused_prec,
            'dcfinder_prec': dcfinder_prec,
            'confused_rec': confused_rec,
            'dcfinder_rec': dcfinder_rec,
        })

    # 输出 LaTeX 表格
    print(r'\begin{table}[ht]')
    print(r'\centering')
    print(r'\caption{Direct Dependency Identification: DCFinder vs. confused vs. pip GT}')
    print(r'\label{tab:direct-dep-comparison}')
    print(r'\resizebox{\textwidth}{!}{')
    print(r'\begin{tabular}{lrrrrrrrrrr}')
    print(r'\toprule')
    print(r'\multirow{2}{*}{Project} & \multicolumn{2}{c}{\# Direct Dependencies} & \multirow{2}{*}{pip (GT)} & \multicolumn{2}{c}{Same (vs.\ pip GT)} & \multicolumn{2}{c}{Precision (\%)} & \multicolumn{2}{c}{Recall (\%)} \\')
    print(r'\cmidrule(lr){2-3} \cmidrule(lr){5-6} \cmidrule(lr){7-8} \cmidrule(lr){9-10}')
    print(r' & confused & DCFinder & & confused & DCFinder & confused & DCFinder & confused & DCFinder \\')
    print(r'\midrule')

    bs = '\\\\'
    for r in results:
        proj = r['project'].replace('_', '\\_')
        print(f"{proj} & {r['confused_total']} & {r['dcfinder_total']} & {r['pip_gt']} "
              f"& {r['confused_same']} & {r['dcfinder_same']} "
              f"& {r['confused_prec']:.1f} & {r['dcfinder_prec']:.1f} "
              f"& {r['confused_rec']:.1f} & {r['dcfinder_rec']:.1f} {bs}")

    # 汇总行（micro-average）
    total_confused = sum(r['confused_total'] for r in results)
    total_dcfinder = sum(r['dcfinder_total'] for r in results)
    total_pip = sum(r['pip_gt'] for r in results)
    total_confused_same = sum(r['confused_same'] for r in results)
    total_dcfinder_same = sum(r['dcfinder_same'] for r in results)
    total_confused_prec = total_confused_same / total_confused * 100 if total_confused else 0
    total_dcfinder_prec = total_dcfinder_same / total_dcfinder * 100 if total_dcfinder else 0
    total_confused_rec = total_confused_same / total_pip * 100 if total_pip else 0
    total_dcfinder_rec = total_dcfinder_same / total_pip * 100 if total_pip else 0

    print(r'\midrule')
    print(f"Total & {total_confused} & {total_dcfinder} & {total_pip} "
          f"& {total_confused_same} & {total_dcfinder_same} "
          f"& {total_confused_prec:.1f} & {total_dcfinder_prec:.1f} "
          f"& {total_confused_rec:.1f} & {total_dcfinder_rec:.1f} \\\\")
    print(r'\bottomrule')
    print(r'\end{tabular}}')
    print(r'\end{table}')

    # 同时输出 CSV 方便查看
    print()
    print('=== CSV format ===')
    print('project,confused_total,dcfinder_total,pip_gt,confused_same,dcfinder_same,confused_prec,dcfinder_prec,confused_rec,dcfinder_rec')
    for r in results:
        print(f"{r['project']},{r['confused_total']},{r['dcfinder_total']},{r['pip_gt']},"
              f"{r['confused_same']},{r['dcfinder_same']},"
              f"{r['confused_prec']:.1f},{r['dcfinder_prec']:.1f},"
              f"{r['confused_rec']:.1f},{r['dcfinder_rec']:.1f}")
    print(f"Total,{total_confused},{total_dcfinder},{total_pip},"
          f"{total_confused_same},{total_dcfinder_same},"
          f"{total_confused_prec:.1f},{total_dcfinder_prec:.1f},"
          f"{total_confused_rec:.1f},{total_dcfinder_rec:.1f}")


if __name__ == '__main__':
    main()
