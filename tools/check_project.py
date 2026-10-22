#!/usr/bin/env python3
"""Offline syntax, source-path and current-document checks; never contact a host."""
import ast
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache', 'outputs'}


class UniqueLoader(yaml.SafeLoader):
    """Reject silent overrides caused by duplicate YAML keys."""


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'duplicate key {key!r}, line {key_node.start_mark.line + 1}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def files(suffix):
    return sorted(p for p in ROOT.rglob('*'+suffix) if not SKIP.intersection(p.relative_to(ROOT).parts))


def mappings(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from mappings(child)


def main():
    errors = []
    counts = dict(python_files=0, yaml_files=0, local_path_references=0,
                  document_links=0, bash_blocks=0, embedded_python_blocks=0)

    def fail(path, detail):
        errors.append(f'{path.relative_to(ROOT)}: {detail}')

    def require(path, target):
        counts['local_path_references'] += 1
        if not target.is_file():
            fail(path, 'missing local source '+str(target))

    for path in files('.py'):
        counts['python_files'] += 1
        try:
            ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            fail(path, str(exc))

    for path in files('.yml'):
        counts['yaml_files'] += 1
        try:
            data = yaml.load(path.read_text(), Loader=UniqueLoader)
        except (ValueError, yaml.YAMLError) as exc:
            fail(path, str(exc))
            continue
        if 'fixtures' in path.parts or 'group_vars' in path.parts:
            continue
        # playbook_dir belongs to the including play, not an included task file.
        context = path.parent
        if path.parent == ROOT/'playbooks/tasks':
            context = ROOT if path.name == 'reset-demo-preflight-tasks.yml' else ROOT/'playbooks'
        for row in mappings(data):
            for key in ('import_playbook', 'include_tasks', 'import_tasks',
                        'ansible.builtin.import_playbook', 'ansible.builtin.include_tasks',
                        'ansible.builtin.import_tasks'):
                value = row.get(key)
                if isinstance(value, str) and '{{' not in value:
                    require(path, path.parent/value)
            for value in row.get('vars_files', []):
                if isinstance(value, str) and '{{' not in value:
                    require(path, path.parent/value)
            script = row.get('ansible.builtin.script')
            if script:
                command = script.get('cmd', '') if isinstance(script, dict) else script
                first = shlex.split(command)[0]
                if '{{' not in first and not Path(first).is_absolute():
                    require(path, path.parent/first)
            copy = row.get('ansible.builtin.copy', {})
            if isinstance(copy, dict) and isinstance(copy.get('src'), str):
                source = copy['src']
                if '{{ item }}' in source and isinstance(row.get('loop'), list):
                    for item in row['loop']:
                        if isinstance(item, str):
                            require(path, path.parent/source.replace('{{ item }}', item))
                elif '{{' not in source and not Path(source).is_absolute():
                    require(path, path.parent/source)
            shell = row.get('ansible.builtin.shell')
            if not isinstance(shell, str):
                continue
            rendered = re.sub(r'{{.*?}}', '0', shell, flags=re.S)
            result = subprocess.run(['bash', '-n'], input=rendered, text=True, capture_output=True)
            counts['bash_blocks'] += 1
            if result.returncode:
                fail(path, 'shell syntax: '+result.stderr.strip())
            lines = rendered.splitlines()
            for i, line in enumerate(lines):
                match = re.search(r"<<\s*['\"]?([A-Z][A-Z0-9_]*)['\"]?", line)
                if not match or 'python' not in line:
                    continue
                end = next((j for j in range(i+1, len(lines)) if lines[j] == match[1]), None)
                if end is None:
                    fail(path, 'missing Python heredoc delimiter '+match[1])
                    continue
                counts['embedded_python_blocks'] += 1
                try:
                    ast.parse('\n'.join(lines[i+1:end]))
                except SyntaxError as exc:
                    fail(path, 'embedded Python: '+str(exc))
        text = path.read_text()
        for pattern in (r'{{\s*playbook_dir\s*}}([^\s\"\x27]+?\.(?:py|yml))',
                        r'playbook_dir\s*\+\s*[\"\x27]([^\"\x27]+\.(?:py|yml))'):
            for match in re.finditer(pattern, text):
                require(path, context/match[1].lstrip('/'))

    for path in files('.md'):
        relative = path.relative_to(ROOT)
        if relative.parts[:2] in (('docs', 'history'), ('archive', 'docs')):
            continue
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
            target = match[1].split('#', 1)[0]
            if not target or re.match(r'[a-zA-Z][\w+.-]*:', target):
                continue
            counts['document_links'] += 1
            if not (path.parent/target).exists():
                fail(path, 'broken document link '+target)
    print(json.dumps(dict(status='FAIL' if errors else 'PASS', **counts, errors=errors), indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
