"""Shared test paths; production code never imports this package."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]


def expanded_tasks(path):
    """Read static task includes in order for migration ordering contracts."""
    import yaml
    path=Path(path)
    def visit(tasks, owner):
        for task in tasks:
            yield task
            include=task.get('ansible.builtin.include_tasks')
            if isinstance(include,str) and '{{' not in include:
                target=owner.parent/include
                yield from visit(yaml.safe_load(target.read_text()),target)
    for play in yaml.safe_load(path.read_text()):
        yield from visit(play['tasks'],path)
