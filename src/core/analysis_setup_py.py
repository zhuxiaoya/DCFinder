
import ast
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

"""
Parse setup.py files.
"""

FIELDS = {
    'author_email',
    'author',
    'classifiers',
    'dependency_links',
    'description',
    'download_url',
    'extras_require',
    'install_requires',
    'keywords',
    'license_file',
    'license',
    'long_description_content_type',
    'long_description',
    'maintainer_email',
    'maintainer',
    'metadata_version',
    'name',
    'obsoletes',
    'package_dir',
    'platforms',
    'project_urls',
    'provides',
    'python_requires',
    'requires',
    'setup_requires',
    'tests_require',
    'url',
    'version',
}


def is_setup_call(element):
    """
    Return if the AST ``element`` is a call to the setup() function.
    Note: this is derived from the code in packagedcode.pypi.py
    """
    if (
        isinstance(element, ast.Call)
        and (
            hasattr(element, 'func')
            and isinstance(element.func, ast.Name)
            and getattr(element.func, 'id', None) == 'setup'
        ) or (
            hasattr(element, 'func')
            and isinstance(element.func, ast.Attribute)
            and getattr(element.func, 'attr', None) == 'setup'
            and isinstance(element.func.value, ast.Name)
            and getattr(element.func.value, 'id', None) == 'setuptools'
        )
    ):
        return True

def parse_setup_py(location):
    """
    Return a mapping of setuptools.setup() call argument found in a setup.py
    file at ``location`` or an empty mapping.
    """
    path = Path(location)
    tree = tuple(ast.parse(path.read_text(encoding='utf8')).body)
    body = tuple(get_body(tree))

    call = get_setup_call(tree)
    result = get_call_kwargs(call, body)

    return clean_setup(result)


def get_body(elements):
    """
    Yield the body from ``elements`` as a single iterable.
    """
    for element in elements:
        if isinstance(element, ast.FunctionDef):
            yield from get_body(element.body)
            continue
        if isinstance(element, ast.If):
            yield from get_body(element.body)
        if isinstance(element, ast.Expr):
            yield element.value
            continue
            # 添加对 Try 语句的处理
        if isinstance(element, ast.Try):
            yield from get_body(element.body)
            continue
        yield element


def get_setup_call(elements):
    """
    Return a setup() method call found in the ``elements`` or None.
    """
    for element in get_body(elements):
        # 检查当前的 element 是否是 setup() 的调用
        if is_setup_call(element):
            return element
        # 如果当前的 element 不是 setup 调用，检查它是否是一个赋值语句（ast.Assign 类型）
        elif isinstance(element, (ast.Assign,)):
            if isinstance(element.value, ast.Call):
                if is_setup_call(element.value):
                    return element.value


def _assign_target_from_value(target, value, local_vars):
    """
    Assign a value to an AST target node, storing in local_vars.
    """
    if isinstance(target, ast.Name):
        local_vars[target.id] = value
    elif isinstance(target, ast.Tuple):
        if isinstance(value, (tuple, list)):
            for sub_target, sub_value in zip(target.elts, value):
                _assign_target_from_value(sub_target, sub_value, local_vars)
        elif isinstance(value, str):
            for sub_target in target.elts:
                _assign_target_from_value(sub_target, value, local_vars)

def node_to_value(node, body, local_vars=None):
    """
    Return the extracted and converted value of a node or None
    """
    local_vars = local_vars or {}
    if node is None:
        return
    if hasattr(ast, 'Constant'):
        if isinstance(node, ast.Constant):
            return node.value

    if isinstance(node, ast.Str):
        return node.s

    if isinstance(node, ast.Num):
        return node.n

    if isinstance(node, (ast.List, ast.Tuple, ast.Set,)):
        return [node_to_value(subnode, body, local_vars) for subnode in node.elts]

    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            result[node_to_value(key, body, local_vars)] = node_to_value(value, body, local_vars)
        return result

    if isinstance(node, ast.Name):
        if node.id in local_vars:
            return local_vars[node.id]
        variable = find_variable_in_body(body, node.id)
        if variable is not None:
            return node_to_value(variable, body, local_vars)

    if isinstance(node, ast.Subscript):
        value = node_to_value(node.value, body, local_vars)
        slice_val = node_to_value(node.slice, body, local_vars)
        if isinstance(value, dict) and slice_val is not None:
            return value.get(slice_val)
        elif isinstance(value, list) and isinstance(slice_val, int):
            return value[slice_val]

    if isinstance(node, ast.DictComp):
        result = {}
        for gen in node.generators:
            iter_val = node_to_value(gen.iter, body)
            if iter_val is None:
                return None
            for item in iter_val:
                local_vars = {}
                _assign_target_from_value(gen.target, item, local_vars)
                key = node_to_value(node.key, body, local_vars)
                val = node_to_value(node.value, body, local_vars)
                if key is not None and val is not None:
                    result[key] = val
        return result

    if isinstance(node, ast.GeneratorExp):
        result = []
        for gen in node.generators:
            iter_val = node_to_value(gen.iter, body)
            if iter_val is None:
                return None
            for item in iter_val:
                local_vars = {}
                _assign_target_from_value(gen.target, item, local_vars)
                val = node_to_value(node.elt, body, local_vars)
                if val is not None:
                    result.append(val)
        return result

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id == 'dict':
                return get_call_kwargs(node, body, local_vars)
            elif node.func.id == 'list':
                if node.args:
                    arg_val = node_to_value(node.args[0], body, local_vars)
                    if arg_val is not None:
                        return list(arg_val)
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 're':
                if node.func.attr == 'findall':
                    pattern_arg = node_to_value(node.args[0], body, local_vars)
                    string_arg = node_to_value(node.args[1], body, local_vars)
                    if pattern_arg and string_arg:
                        import re
                        return re.findall(pattern_arg, string_arg)
        return
    return


def find_variable_in_body(body, name):
    """
    Return the value of the variable ``name`` found in the ``body`` ast tree or None.
    """
    for elem in body:
        if not isinstance(elem, ast.Assign):
            continue
        for target in elem.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == name:
                return elem.value

def get_call_kwargs(node: ast.Call, body, local_vars=None):
    """
    Return a mapping of setup() method call keyword arguments.
    """
    local_vars = local_vars or {}
    result = {}
    keywords = getattr(node, 'keywords', []) or []
    for keyword in keywords:
        # dict unpacking
        if keyword.arg is None:
            value = node_to_value(keyword.value, body, local_vars)
            if isinstance(value, dict):
                result.update(value)
            continue
        # keyword argument
        value = node_to_value(keyword.value, body, local_vars)
        if value is None:
            continue
        result[keyword.arg] = value
    return result


def clean_setup(data):
    """
    Return a cleaned mapping from a setup ``data`` mapping.
    """
    result = {k: v
        for k, v in data.items()
        if k in FIELDS
        and (v and v is not False)
        and str(v) != 'UNKNOWN'
    }

    # split keywords in words
    keywords = result.get('keywords')
    if keywords and isinstance(keywords, str):
        # some keywords are separated by coma, some by space or lines
        if ',' in keywords:
            keywords = [k.strip() for k in keywords.split(',')]
        else:
            keywords = keywords.split()
        result['keywords'] = keywords

    return result


def dependency_dict(name, specifier='', raw_requirement='', source_file=''):
    return {
        'name': canonicalize_name(name),
        'specifier': str(specifier) if specifier else '',
        'raw_requirement': raw_requirement or name,
        'source_file': source_file,
    }


def dependency_from_requirement_string(req_string, source_file=''):
    req_string = (req_string or '').strip()
    if not req_string or req_string.startswith('#'):
        return None
    if 'https://github.com' in req_string:
        return None
    if '--index-url' in req_string or '--extra-index-url' in req_string:
        parts = (
            req_string.split('--index-url')
            if '--index-url' in req_string
            else req_string.split('--extra-index-url')
        )
        req_string = parts[0].strip()
    try:
        req = Requirement(req_string)
    except Exception:
        return None
    if not req.name:
        return None
    return dependency_dict(
        name=req.name,
        specifier=str(req.specifier),
        raw_requirement=req_string,
        source_file=source_file,
    )


def _collect_setup_requirements(setup_args):
    """
    从 parse_setup_py 结果中收集所有类型依赖及其类型。
    
    返回：[(requirement_string, dependency_type), ...]
    dependency_type:
        - runtime: 安装依赖（install_requires）
        - test: 测试依赖（tests_require）
        - setup: 构建依赖（setup_requires）
        - extra:<name>: 可选依赖（extras_require）
    """
    requires = []
    scope_by_key = {
        "install_requires": "runtime",
        "tests_require": "test",
        "setup_requires": "setup",
    }
    for key, dep_type in scope_by_key.items():
        value = setup_args.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            for v in value:
                requires.append((v, dep_type))
        elif isinstance(value, str):
            requires.append((value, dep_type))

    extras = setup_args.get('extras_require') or {}
    if isinstance(extras, dict):
        for extra_name, extra_reqs in extras.items():
            if isinstance(extra_reqs, (list, tuple)):
                for req in extra_reqs:
                    requires.append((req, f'extra:{extra_name}'))
            elif isinstance(extra_reqs, str):
                requires.append((extra_reqs, f'extra:{extra_name}'))
    return requires


def get_setup_py_python_requires(setup_args):
    """
    Return a mapping of {python_requires: value} or an empty mapping found in a
    ``setup_args`` mapping of setup.py arguments.
    """
    python_requires = setup_args.get('python_requires')
    if python_requires:
        return dict(python_requires=python_requires)
    else:
        return {}

def get_setup_py_dependencies(location):
    setup_args = parse_setup_py(location)
    dependent_packages = []
    for req, dep_type in _collect_setup_requirements(setup_args):
        if not isinstance(req, str):
            continue
        dep = dependency_from_requirement_string(req, source_file=location)
        if dep:
            dep['dependency_type'] = dep_type
            dependent_packages.append(dep)

    return dependent_packages

def extract_metadata_dependencies(location):
    metadata=None

    #提取setuptools.setup()内容
    setup_args = parse_setup_py(location)
    softwareName=setup_args.get('name')
    softwareVersion=setup_args.get('version')
    dependencies = get_setup_py_dependencies(setup_args)

    return dependencies




if __name__=="__main__":
    # location=r'/home/lab314/zxy/Project_progress/python_project/Parselmouth/setup.py'
    location=r'/home/lab314/zxy/Project_progress/python_project/flintrock-master/setup.py'
    # location=r'/home/lab314/zxy/Project_progress/python_project/carbon/setup.py'

    dependencies=extract_metadata_dependencies(location)
    print(dependencies)