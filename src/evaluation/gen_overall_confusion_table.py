"""
生成总体依赖混淆检测对比 LaTeX 表格
用法: python3 gen_overall_confusion_table.py
输出: LaTeX 表格 (tab:confusion_overall)

逻辑说明：
  DCFinder 的 dependency_confusion.txt 已独立验证包在私有源存在，故：
    TP = DCFinder types[2]/[3] ∩ pip download_source='pypi'（不查 private_exists）
    FP = DCFinder types[2]/[3] ∩ pip download_source='private'
    FN = pip pypi + private_exists=True − DCFinder 所有混淆包（查 private_exists）
  pip GT = TP + FN
  
  Direct/Indirect 拆分：DCFinder 按 types[2]/[3]；pip GT 按 is_direct 去重
  Confused / Combobulator 均为 0
"""
import os, json, csv
from packaging.utils import canonicalize_name

DCFINDER_BASE = '/home/lab314/zxy/dependncy_confusion/comparison_results_v4'
DRYRUN_BASE = '/home/lab314/zxy/dependncy_confusion/dynamic_results_all_dryrun_v4'


def get_dcfinder_confused(project):
    """DCFinder 混淆包 (types[2]=direct, types[3]=indirect)"""
    d_direct = {}
    d_indirect = {}
    path = os.path.join(DCFINDER_BASE, project, 'expanded', 'dependency_confusion.txt')
    if not os.path.exists(path):
        return d_direct, d_indirect
    with open(path) as f:
        data = json.load(f)
    for cat, target in [
        ('direct dependency with same name', d_direct),
        ('indirect private dependency with same name', d_indirect),
    ]:
        section = data.get(cat, {})
        pkgs = section.get('packages', {})
        if isinstance(pkgs, dict):
            for name in pkgs:
                target[canonicalize_name(name)] = True
    return d_direct, d_indirect


def get_pip_gt_per_project(project):
    """pip GT: 按 (project, package) 去重后分 direct/indirect
    Returns: (pip_pypi_all, pip_private_all, gt_direct, gt_indirect)
      pip_pypi_all = {pkg} pip从PyPI下载 (不查private_exists, 给TP用)
      pip_private_all = {pkg} pip从私有源下载 (给FP用)
      gt_direct = {pkg} pip pypi + private_exists=True + is_direct (给FN用)
      gt_indirect = {pkg} pip pypi + private_exists=True + is_indirect (给FN用)
    
    去重: is_direct 取 OR; download_source/private_exists 最后一行覆盖
    """
    pkg_is_direct = {}
    pkg_src = {}
    pkg_priv = {}

    csv_path = os.path.join(DRYRUN_BASE, 'all_deps_detail.csv')
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if row['project'] != project:
                    continue
                pkg = canonicalize_name(row['package'])
                src = row.get('download_source', '').lower()
                priv_exists = row.get('private_exists', '').lower() == 'true'
                is_direct = row.get('is_direct', '') == 'True'

                if pkg not in pkg_is_direct:
                    pkg_is_direct[pkg] = False
                if is_direct:
                    pkg_is_direct[pkg] = True
                pkg_src[pkg] = src
                pkg_priv[pkg] = priv_exists

    pip_pypi_all = set()
    pip_private_all = set()
    gt_direct = set()
    gt_indirect = set()

    for pkg, src in pkg_src.items():
        if src == 'pypi':
            pip_pypi_all.add(pkg)
            if pkg_priv.get(pkg, False):
                if pkg_is_direct.get(pkg, False):
                    gt_direct.add(pkg)
                else:
                    gt_indirect.add(pkg)
        elif src == 'private':
            pip_private_all.add(pkg)

    return pip_pypi_all, pip_private_all, gt_direct, gt_indirect


def main():
    projects = sorted([p for p in os.listdir(DCFINDER_BASE)
                      if os.path.isdir(os.path.join(DCFINDER_BASE, p)) and p != 'logs'])

    stats = {
        'direct': {'dcf': 0, 'pip_gt': 0, 'tp': 0, 'fp': 0, 'fn': 0},
        'indirect': {'dcf': 0, 'pip_gt': 0, 'tp': 0, 'fp': 0, 'fn': 0},
    }

    for project in projects:
        d_direct, d_indirect = get_dcfinder_confused(project)
        pip_pypi_all, pip_private_all, gt_d, gt_i = get_pip_gt_per_project(project)

        dcf_direct = set(d_direct.keys())
        dcf_indirect = set(d_indirect.keys())
        all_dcf = dcf_direct | dcf_indirect

        # ── Direct scope ──
        tp_d = dcf_direct & pip_pypi_all       # DCF已验证私有存在, 不查private_exists
        fp_d = dcf_direct & pip_private_all
        fn_d = gt_d - all_dcf                  # 需查private_exists

        stats['direct']['dcf'] += len(dcf_direct)
        stats['direct']['pip_gt'] += len(gt_d)
        stats['direct']['tp'] += len(tp_d)
        stats['direct']['fp'] += len(fp_d)
        stats['direct']['fn'] += len(fn_d)

        # ── Indirect scope ──
        tp_i = dcf_indirect & pip_pypi_all
        fp_i = dcf_indirect & pip_private_all
        fn_i = gt_i - all_dcf

        stats['indirect']['dcf'] += len(dcf_indirect)
        stats['indirect']['pip_gt'] += len(gt_i)
        stats['indirect']['tp'] += len(tp_i)
        stats['indirect']['fp'] += len(fp_i)
        stats['indirect']['fn'] += len(fn_i)

    # ── pip GT = TP + FN (not independent pypi+priv count) ──
    for scope in ('direct', 'indirect'):
        s = stats[scope]
        s['pip_gt'] = s['tp'] + s['fn']  # Effective pip GT for this scope
        s['prec'] = s['tp'] / (s['tp'] + s['fp']) * 100 if (s['tp'] + s['fp']) > 0 else 0
        s['rec'] = s['tp'] / (s['tp'] + s['fn']) * 100 if (s['tp'] + s['fn']) > 0 else 0

    # ── Overall ──
    ov = {}
    for k in ('dcf', 'tp', 'fp', 'fn'):
        ov[k] = stats['direct'][k] + stats['indirect'][k]
    ov['pip_gt'] = ov['tp'] + ov['fn']
    ov['prec'] = ov['tp'] / (ov['tp'] + ov['fp']) * 100 if (ov['tp'] + ov['fp']) > 0 else 0
    ov['rec'] = ov['tp'] / (ov['tp'] + ov['fn']) * 100 if (ov['tp'] + ov['fn']) > 0 else 0

    # ── LaTeX ──
    s_d = stats['direct']
    s_i = stats['indirect']

    print(r'\begin{table*}[t]')
    print(r' \centering')
    print(r' \caption{Overall comparison of dependency-confusion detection results.}')
    print(r' \label{tab:confusion_overall}')
    print(r' ')
    print(r' \begin{tabular*}{0.95\textwidth}{')
    print(r' @{\extracolsep{\fill}}')
    print(r' cccccccccc')
    print(r' @{}')
    print(r' }')
    print(r' \toprule')
    print(r' \shortstack{Dependency\\scope}')
    print(r' & DCFinder & Confused & Combobulator & pip')
    print(r' & TP & FP & FN & Precision & Recall \\ \midrule')
    print(r' ')
    print(f' Direct & {s_d["dcf"]:,} & 0 & 0 & {s_d["pip_gt"]:,}')
    print(f' & {s_d["tp"]:,}')
    print(f' & {s_d["fp"]:,}')
    print(f' & {s_d["fn"]:,}')
    print(f' & {s_d["prec"]:.1f}')
    print(f' & {s_d["rec"]:.1f} \\\\')
    print(r' ')
    print(f' Indirect & {s_i["dcf"]:,} & 0 & 0 & {s_i["pip_gt"]:,}')
    print(f' & {s_i["tp"]:,}')
    print(f' & {s_i["fp"]:,}')
    print(f' & {s_i["fn"]:,}')
    print(f' & {s_i["prec"]:.1f}')
    print(f' & {s_i["rec"]:.1f} \\\\')
    print(r' ')
    print(r' \midrule')
    print(f' \textbf{{Overall}} & \textbf{{{ov["dcf"]:,}}} & \textbf{{0}} & \textbf{{0}} & \textbf{{{ov["pip_gt"]:,}}}')
    print(f' & \textbf{{{ov["tp"]:,}}}')
    print(f' & \textbf{{{ov["fp"]:,}}}')
    print(f' & \textbf{{{ov["fn"]:,}}}')
    print(f' & \textbf{{{ov["prec"]:.1f}}}')
    print(f' & \textbf{{{ov["rec"]:.1f}}} \\\\')
    print(r' ')
    print(r' \bottomrule')
    print(r' \end{tabular*}')
    print(r'\end{table*}')

    # ── CSV + 检查 ──
    print()
    print('% === CSV ===')
    for scope, s in [('direct', s_d), ('indirect', s_i), ('overall', ov)]:
        print(f'{scope},{s["dcf"]},0,0,{s["pip_gt"]},{s["tp"]},{s["fp"]},{s["fn"]},{s["prec"]:.1f},{s["rec"]:.1f}')

    print()
    print('% === Checks ===')
    for scope in ('direct', 'indirect'):
        s = stats[scope]
        dcf_gap = s['dcf'] - s['tp'] - s['fp']
        print(f'% {scope}: tp+fp={s["tp"]+s["fp"]} vs dcf={s["dcf"]} (unverifiable={dcf_gap}), '
              f'pip={s["pip_gt"]}')
    print(f'% Overall: TP={ov["tp"]}, FP={ov["fp"]}, FN={ov["fn"]}, '
          f'pip={ov["pip_gt"]}, Prec/Rec={ov["prec"]:.1f}/{ov["rec"]:.1f}')


if __name__ == '__main__':
    main()
