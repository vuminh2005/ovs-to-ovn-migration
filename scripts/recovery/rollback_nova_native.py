#!/usr/bin/env python3
"""Read only Nova API/cell DBs inside nova_api; no HTTP, Neutron, RPC or writes."""
import json
import re
import signal

CELLS = 'SELECT id, uuid, database_connection FROM cell_mappings ORDER BY id'
MAPPINGS = ('SELECT instance_uuid, cell_id FROM instance_mappings '
            'WHERE queued_for_delete = 0 OR queued_for_delete IS NULL ORDER BY instance_uuid')
INSTANCES = 'SELECT uuid, host, vm_state, task_state FROM instances WHERE deleted = 0 ORDER BY uuid'
STATES = {'active': 'ACTIVE', 'stopped': 'SHUTOFF', 'paused': 'PAUSED', 'suspended': 'SUSPENDED'}


class ReadStop(RuntimeError):
    """Only authored messages may leave the container."""


def read_rows(url, statements):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    if make_url(url).get_backend_name() not in ('mysql', 'mariadb'):
        raise ReadStop('Native Nova checkpoint requires MySQL/MariaDB cell databases')
    engine = create_engine(url, future=True, poolclass=NullPool, echo=False,
                           connect_args={'connect_timeout': 5})
    try:
        with engine.connect() as connection:
            connection.execute(text('SET SESSION TRANSACTION READ ONLY'))
            connection.commit()
            with connection.begin():
                return [[dict(row) for row in connection.execute(text(sql)).mappings()] for sql in statements]
    finally:
        engine.dispose()


def snapshot(api_url):
    cells, mappings = read_rows(api_url, [CELLS, MAPPINGS])
    if not cells or len({cell['id'] for cell in cells}) != len(cells):
        raise ReadStop('Nova cell coverage is empty or ambiguous')
    known = {cell['id'] for cell in cells}
    expected = {}
    for mapping in mappings:
        if mapping['cell_id'] not in known or mapping['instance_uuid'] in expected:
            raise ReadStop('Nova has an unassigned, duplicate or unknown instance mapping')
        expected[mapping['instance_uuid']] = mapping['cell_id']
    found = {}
    rows = []
    for cell in cells:
        # Include cell0/disabled cells. Unreachable cells must never imply no VMs.
        for instance in read_rows(cell['database_connection'], [INSTANCES])[0]:
            identifier = instance['uuid']
            if (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f-]{36}', identifier) or
                    identifier in found or expected.get(identifier) != cell['id']):
                raise ReadStop('Nova instance UUID/cell mapping is incomplete or inconsistent')
            if (instance['task_state'] is not None or instance['vm_state'] not in STATES or
                    not isinstance(instance['host'], str) or not instance['host']):
                raise ReadStop('Nova has an in-flight or unsupported instance state')
            found[identifier] = cell['id']
            rows.append(dict(id=identifier, compute_host=instance['host'],
                             status=STATES[instance['vm_state']], task_state=None))
    if found != expected:
        raise ReadStop('Nova API mappings differ from the complete cell instance inventory')
    # URLs remain private and are compared in memory; never serialize them.
    return dict(cells=cells, mappings=mappings, servers=sorted(rows, key=lambda row: row['id']))


def observe(api_url):
    first = snapshot(api_url)
    if snapshot(api_url) != first:
        raise ReadStop('Nova changed while its read-only checkpoint was being collected')
    return dict(schema=1, status='PASS', source='nova-cell-db', servers=first['servers'],
                cells=sorted(cell['uuid'] for cell in first['cells']))


def main():
    def deadline(signum, frame):
        raise ReadStop('Native Nova read-only checkpoint timed out')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(60)
    try:
        from nova import config, conf, version
        release = version.version_string()
        if not re.fullmatch(r'29\.[0-9A-Za-z.+-]+', release):
            raise ReadStop('Native Nova checkpoint supports the 2024.1 Nova 29 series')
        config.parse_args(['rollback-nova-read', '--config-file', '/etc/nova/nova.conf'],
                          configure_db=False, init_rpc=False)
        result = dict(observe(conf.CONF.api_database.connection), nova_version=release)
    except Exception as exc:
        result = dict(status='STOP', error_type=type(exc).__name__)
        result['reason'] = str(exc) if isinstance(exc, ReadStop) else 'Cannot read every Nova cell; check native configuration and database availability'
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
