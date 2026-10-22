#!/usr/bin/env python3
"""Explicitly raise only the authenticated project's instance quota when needed."""
import argparse
import json
from pathlib import Path
import sys

from run_layout import run_path


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def values(quota):
    result = {key: getattr(quota, key) for key in ('instances', 'cores', 'ram')}
    if any(type(value) is not int or value < -1 for value in result.values()):
        raise ValueError('Malformed project quota response')
    return result


def ensure_instance_quota(cloud, root, limit=20):
    if type(limit) is not int or limit <= 0:
        raise ValueError('Instance quota limit must be a positive integer')
    project = cloud.session.get_project_id()
    if not project:
        raise RuntimeError('Quota update requires project-scoped authentication')
    before = values(cloud.compute.get_quota_set(project))
    path = run_path(Path(root), 'quota-instances.json')
    result = dict(project=project, requested_limit=limit, before=before,
                  after=dict(before), changed=False, update_attempted=False, status='PASS')
    if before['instances'] != -1 and before['instances'] < limit:
        result.update(status='IN_PROGRESS', update_attempted=True)
        save(path, result)
        try:
            # Explicit URI project_id also supports older SDK quota update paths.
            cloud.compute.update_quota_set(project, project_id=project, instances=limit)
            result.update(after=values(cloud.compute.get_quota_set(project)), changed=True)
            after = result['after']
            if (after['instances'] != -1 and after['instances'] < limit or
                    any(after[key] != before[key] for key in ('cores', 'ram'))):
                raise RuntimeError('Quota verification failed: instances too small or cores/RAM changed')
            result['status'] = 'PASS'
        except Exception as exc:
            result.update(status='FAIL', error_type=type(exc).__name__)
            save(path, result)
            raise
    save(path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error('--limit must be a positive integer')
    import openstack
    try:
        result = ensure_instance_quota(openstack.connect(), args.root, args.limit)
    except Exception as exc:
        print(f'Instance quota operation failed ({type(exc).__name__}); inspect '
              f'{run_path(args.root, "quota-instances.json")}', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
