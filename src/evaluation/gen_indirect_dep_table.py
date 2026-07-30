import os, csv
from packaging.utils import canonicalize_name

DCFINDER_BASE = '/home/lab314/zxy/dependncy_confusion/comparison_results_v4'
DRYRUN_BASE = '/home/lab314/zxy/dependncy_confusion/dynamic_results_all_dryrun_v4'


def get_dcfinder_indirect(project):
    """DCFinder 间接依赖"""
    deps = set()
    f = os.path.join(DCFINDER_BASE, project, 'expanded', 'indirect_dependency_packages.txt')
    if os.path.exists(f):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#'):
                    name = line.split(' ')[0]
                    deps.add(canonicalize_name(name))
    return deps


def get_pip_indirect(project):
    """pip dry-run 间接依赖 (is_direct=False) + 补充GT间接依赖"""
    deps = set()
    # 1. pip dry-run 实际安装结果
    with open(os.path.join(DRYRUN_BASE, 'all_deps_detail.csv')) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['project'] == project and row.get('is_direct', '').lower() == 'false':
                pkg = row.get('package', '').strip()
                if pkg:
                    deps.add(canonicalize_name(pkg))
    # 2. 补充 GT 间接依赖 (pip dry-run 失败文件的间接依赖)
    supp_path = os.path.join(DRYRUN_BASE, 'supplementary_gt.csv')
    if os.path.exists(supp_path):
        with open(supp_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['project'] == project and row.get('is_direct', '').lower() == 'false':
                    pkg = row.get('package', '').strip()
                    if pkg:
                        deps.add(canonicalize_name(pkg))
    return deps


def main():
    projects = [p for p in os.listdir(DCFINDER_BASE)
                if os.path.isdir(os.path.join(DCFINDER_BASE, p)) and p != 'logs']

    results = []
    for proj in sorted(projects):
        dcf = get_dcfinder_indirect(proj)
        pip_gt = get_pip_indirect(proj)
        if not dcf and not pip_gt:
            continue
        same = dcf & pip_gt
        dcf_only = dcf - pip_gt
        pip_only = pip_gt - dcf
        dcf_prec = len(same) / len(dcf) * 100 if dcf else 0
        dcf_rec = len(same) / len(pip_gt) * 100 if pip_gt else 0
        results.append({
            'project': proj,
            'dcf_total': len(dcf),
            'pip_gt': len(pip_gt),
            'same': len(same),
            'dcf_only': len(dcf_only),
            'pip_only': len(pip_only),
            'dcf_prec': dcf_prec,
            'dcf_rec': dcf_rec,
        })

    # 输出 LaTeX 表格
    print(r'\begin{table}[ht]')
    print(r'\centering')
    print(r'\caption{Indirect Dependency Identification: DCFinder vs. pip GT}')
    print(r'\label{tab:indirect-dep-comparison}')
    print(r'\resizebox{\textwidth}{!}{')
    print(r'\begin{tabular}{lrrrrrrr}')
    print(r'\toprule')
    print(r'Project & DCFinder & pip (GT) & Same & DCFinder-only & pip-only & Precision (\%) & Recall (\%) \\')
    print(r'\midrule')

    bs = '\\\\'
    for r in results:
        proj = r['project'].replace('_', '\\_')
        print(f"{proj} & {r['dcf_total']} & {r['pip_gt']} & {r['same']} "
              f"& {r['dcf_only']} & {r['pip_only']} "
              f"& {r['dcf_prec']:.1f} & {r['dcf_rec']:.1f} {bs}")

    total_dcf = sum(r['dcf_total'] for r in results)
    total_pip = sum(r['pip_gt'] for r in results)
    total_same = sum(r['same'] for r in results)
    total_dcf_only = sum(r['dcf_only'] for r in results)
    total_pip_only = sum(r['pip_only'] for r in results)
    total_prec = total_same / total_dcf * 100 if total_dcf else 0
    total_rec = total_same / total_pip * 100 if total_pip else 0

    print(r'\midrule')
    print(f"Total & {total_dcf} & {total_pip} & {total_same} "
          f"& {total_dcf_only} & {total_pip_only} "
          f"& {total_prec:.1f} & {total_rec:.1f} \\\\")
    print(r'\bottomrule')
    print(r'\end{tabular}}')
    print(r'\end{table}')

    # CSV 格式
    print()
    print('=== CSV format ===')
    print('project,dcfinder_total,pip_gt,same,dcfinder_only,pip_only,dcfinder_prec,dcfinder_rec')
    for r in results:
        print(f"{r['project']},{r['dcf_total']},{r['pip_gt']},{r['same']},"
              f"{r['dcf_only']},{r['pip_only']},"
              f"{r['dcf_prec']:.1f},{r['dcf_rec']:.1f}")
    print(f"Total,{total_dcf},{total_pip},{total_same},"
          f"{total_dcf_only},{total_pip_only},"
          f"{total_prec:.1f},{total_rec:.1f}")


if __name__ == '__main__':
    main()
