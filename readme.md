# DCFinder — A Dependency Confusion Detection Method Based on Multi-Dimensional Dependency Analysis

Artifact for the paper "DCFinder: A Dependency Confusion Detection Method Based on Multi-Dimensional Dependency Analysis".

---

## Directory Structure

```
release/
├── readme.md
│
├── src/
│   ├── core/                                    # DCFinder engine + Pip ground truth
│   │   ├── README.md
│   │   ├── detect_dependency_confusion_v4.py    #   Core DCFinder engine (2,737 lines)
│   │   ├── analysis_setup_py.py                 #   AST parser for setup.py
│   │   ├── run_pip_dryrun.py                    #   Pip dry-run executor
│   │   ├── supplement_pip_gt.py                 #   Supplement direct dependency GT
│   │   └── supplement_indirect_gt.py            #   Supplement indirect dependency GT
│   │
│   ├── dataPreparation/                         # Dataset construction pipeline
│   │   ├── README.md
│   │   ├── download_top_python_projects.py      #   GitHub API: download ~1,000 projects
│   │   ├── analysis_program_extra_url.py        #   Extract projects with non-PyPI URLs
│   │   └── analysis_url_inaccessible_get.py     #   URL reachability checker
│   │
│   └── evaluation/                              # Analysis & result generation
│       ├── README.md
│       ├── gen_overall_confusion_table.py       #   Overall confusion detection result
│       ├── gen_direct_dep_table.py              #   Direct dependency identification result
│       ├── gen_indirect_dep_table.py            #   Indirect dependency identification result
│       └── run_total_confusion.py               #   Per-project confusion statistics
│
├── data/
│   ├── dcfinder_results/                        # DCFinder output (28 projects × 2 modes)
│   │   └── <project>/
│   │       ├── expanded/
│   │       │   ├── dependency_confusion.txt
│   │       │   ├── same_name_dependencies.txt
│   │       │   ├── direct_dependency_packages.txt
│   │       │   ├── indirect_dependency_packages.txt
│   │       │   ├── direct_by_config_file.json
│   │       │   ├── package_details.json
│   │       │   ├── private_sources.txt
│   │       │   ├── traversal_stats.json
│   │       │   └── deps_graph.json
│   │       └── pruned/                          (same structure)
│   │
│   ├── dynamic_reference/                       # Pip dry-run output (28 projects)
│   │   ├── all_deps_detail.csv                  #   Aggregated GT (17,593 rows)
│   │   ├── supplementary_gt.csv                 #   Supplementary GT
│   │   └── <project>/                           #   Per-project: pip_deps_detail.csv,
│   │                                               pip_deps_summary.txt, pip_reports/
│   │
│   ├── non_pypi_urls.txt                        # Non-PyPI index URLs (35 projects)
│   ├── index_reachability.txt                   # URL reachability results
│   └── top_python_projects.txt                  # GitHub project metadata
│
└── results/                                     # Pre-computed evaluation results
    ├── overall_confusion_table.txt
    ├── per_project_confusion.txt
    ├── direct_dep_table.txt
    └── indirect_dep_table.txt
```

---

## Experiment Environment

All experiments were conducted in a fixed Conda environment:

| Item | Specification |
|------|------|
| **OS** | Ubuntu 22.04.5 LTS (x86\_64) |
| **Python** | 3.10.19 |
| **pip** | 26.0.1 |
| **Conda env** | `vfc` |
| **Resolver** | `resolvelib`-based (pip default) |
| **Index config** | `--index-url https://pypi.org/simple/` + project-specific `--extra-index-url` |
| **Pip flags** | `--dry-run --ignore-installed --report` |
| **Network** | Access to PyPI.org and all private indexes listed below |
| **Storage** | ~400 MB for source + data; ~3 GB for cloned benchmark projects |

### Python Dependencies

The Conda environment `vfc` (Python 3.10.19) was used. Besides the Python standard library, only two third-party packages are required by DCFinder:

```
packaging==26.2
requests==2.32.5
```

Setup:

```bash
conda create -n vfc python=3.10.19
conda activate vfc
pip install packaging==26.2 requests==2.32.5
```

---

## Quick Start: Reproduce Results from Pre-Computed Data

```

# Overall confusion detection table (LaTeX)
python release/src/evaluation/gen_overall_confusion_table.py

# Per-project confusion statistics
python release/src/evaluation/run_total_confusion.py

# Direct dependency identification table (LaTeX)
python release/src/evaluation/gen_direct_dep_table.py

# Indirect dependency identification table (LaTeX)
python release/src/evaluation/gen_indirect_dep_table.py
```

Pre-computed outputs are also available in `release/results/`.

---

## Full Reproduction: End-to-End Pipeline

### Stage 1: Dataset Construction (~1,000 → 28 projects)

```bash
python release/src/dataPreparation/download_top_python_projects.py
python release/src/dataPreparation/analysis_program_extra_url.py
python release/src/dataPreparation/analysis_url_inaccessible_get.py
```

**Filtering funnel:**

| Stage | Count | Description |
|-------|:-----:|------|
| Top Python projects considered | 1,000 | GitHub Python projects ranked by stars |
| Valid Python projects with parseable dependency configurations | 901 | Projects satisfying the dependency-configuration screening criteria |
| Projects containing multi-index or non-PyPI index configurations | **35** | Configurations containing `--extra-index-url` or other non-PyPI package-index URLs |
| Projects with reachable additional indexes | **30** | 5 projects excluded because the configured endpoints were inaccessible, non-operational, or placeholder/example endpoints |
| **Final benchmark projects** | **28** | 2 additional projects excluded because their configured non-PyPI endpoints were public PyPI mirrors |

### Stage 2: Run DCFinder

```bash
python release/src/core/detect_dependency_confusion.py
# Output: data/dcfinder_results/<project>/expanded/ and pruned/
```

### Stage 3: Construct Pip Ground Truth

```bash
python release/src/core/run_pip_dryrun.py           # Phase 1: pip dry-run
python release/src/core/supplement_pip_gt.py         # Phase 2: supplement direct deps
python release/src/core/supplement_indirect_gt.py    # Phase 3: supplement indirect deps
```

- **Pip strategy**: Full-file resolution → per-package fallback for failed configuration files

### Stage 4: Generate Evaluation Tables

```bash
python release/src/evaluation/gen_overall_confusion_table.py
python release/src/evaluation/gen_direct_dep_table.py
python release/src/evaluation/gen_indirect_dep_table.py
```

---


