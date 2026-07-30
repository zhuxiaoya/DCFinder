import os, json, csv
from packaging.utils import canonicalize_name
from packaging.version import parse as parse_version

DCFINDER_BASE = 'comparison_results_v4'
DRYRUN_BASE = 'dynamic_results_all_dryrun_v4'

def safe_version(v):
    try:
        return parse_version(v)
    except:
        return None

def get_dcf_confused(project):
    direct_confused = {}
    indirect_confused = {}
    path = os.path.join(DCFINDER_BASE, project, 'expanded', 'dependency_confusion.txt')
    if not os.path.exists(path):
        return direct_confused, indirect_confused
    with open(path) as f:
        data = json.load(f)
    for cat, target in [('direct dependency with same name', direct_confused),
                        ('indirect private dependency with same name', indirect_confused)]:
        section = data.get(cat, {})
        pkgs = section.get('packages', {})
        if isinstance(pkgs, dict):
            for name, info in pkgs.items():
                target[canonicalize_name(name)] = {
                    'private_version': info.get('private version', ''),
                    'public_version': info.get('public version', ''),
                }
    return direct_confused, indirect_confused

def get_pip_info(project):
    info = {}
    csv_path = os.path.join(DRYRUN_BASE, 'all_deps_detail.csv')
    if not os.path.exists(csv_path):
        return info
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            if row['project'] != project:
                continue
            pkg = canonicalize_name(row['package'])
            info[pkg] = {
                'download_source': row.get('download_source', '').lower(),
                'is_direct': row.get('is_direct', '') == 'True',
                'private_exists': row.get('private_exists', '').lower() == 'true',
            }
    return info

projects = sorted([p for p in os.listdir(DCFINDER_BASE)
                   if os.path.isdir(os.path.join(DCFINDER_BASE, p)) and p != 'logs'])

print(f"{'Project':<40} {'DCF':>6} {'pipGT':>6} {'TP':>5} {'FP':>5} {'FN':>5} {'TN':>5} {'Prec':>7} {'Rec':>7}")
print('-' * 100)

total = {'tp':0,'fp':0,'fn':0,'tn':0,'dcf':0,'pip_gt':0}

for p in projects:
    dcf_d, dcf_i = get_dcf_confused(p)
    all_dcf = {}
    all_dcf.update(dcf_d)
    all_dcf.update(dcf_i)
    pip = get_pip_info(p)

    tp=fp=fn=tn=0
    for pkg, dcf_info in all_dcf.items():
        priv_v = safe_version(dcf_info['private_version'])
        pub_v = safe_version(dcf_info['public_version'])
        is_confused = False
        if priv_v and pub_v:
            is_confused = pub_v >= priv_v
        else:
            is_confused = True

        if not is_confused:
            continue
        pip_data = pip.get(pkg, {})
        src = pip_data.get('download_source','')
        if src == 'pypi':
            tp += 1
        elif src == 'private':
            fp += 1

    for pkg, dl in pip.items():
        if pkg in all_dcf:
            continue
        if dl['download_source'] == 'pypi' and dl.get('private_exists'):
            fn += 1
        elif dl['download_source'] == 'private':
            tn += 1

    pip_gt = tp + fn
    prec = tp/(tp+fp)*100 if (tp+fp)>0 else 0
    rec = tp/(tp+fn)*100 if (tp+fn)>0 else 0
    total['tp']+=tp; total['fp']+=fp; total['fn']+=fn; total['tn']+=tn
    total['dcf']+=len(all_dcf); total['pip_gt']+=pip_gt
    print(f'{p:<40} {len(all_dcf):>6} {pip_gt:>6} {tp:>5} {fp:>5} {fn:>5} {tn:>5} {prec:>6.1f}% {rec:>6.1f}%')

print('-' * 100)
t=total
tp=t['tp']; fp=t['fp']; fn=t['fn']; tn=t['tn']
prec=tp/(tp+fp)*100 if (tp+fp)>0 else 0
rec=tp/(tp+fn)*100 if (tp+fn)>0 else 0
print(f'{"Total":<40} {t["dcf"]:>6} {t["pip_gt"]:>6} {tp:>5} {fp:>5} {fn:>5} {tn:>5} {prec:>6.1f}% {rec:>6.1f}%')
