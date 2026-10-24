#!/usr/bin/env python3
"""Neutron-only logical checkpoint and isolated same-image restore rehearsal.

Runs on the selected MariaDB host. Credentials never appear in argv or JSON.
No MariaDB service/volume is stopped, replaced or restored wholesale.
"""
import sys
from pathlib import Path
if (Path(__file__).resolve().parents[1] / '_paths.py').is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _paths import configure
    configure()
import argparse
from contextlib import contextmanager
import configparser
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from rollback_common import Stop, business_sha, command, journal, owned, save, sha

GALERA = Path('/etc/kolla/mariadb/galera.cnf')
INIT = '''set -euo pipefail
mkdir -p /tmp/rollback-db
chown mysql:mysql /tmp/rollback-db
mariadb-install-db --no-defaults --datadir=/tmp/rollback-db --auth-root-authentication-method=normal --skip-test-db --user=mysql >/tmp/install.log 2>&1
exec mariadbd --no-defaults --user=mysql --datadir=/tmp/rollback-db --socket=/tmp/rollback.sock --pid-file=/tmp/rollback.pid --skip-networking --skip-log-bin
'''
DUMP = ['--single-transaction', '--quick', '--hex-blob', '--skip-lock-tables', '--default-character-set=utf8mb4',
        '--skip-comments', '--skip-dump-date', '--skip-extended-insert', '--order-by-primary',
        '--skip-triggers', '--skip-events', '--skip-routines', '--databases']


def sql_name(name):
    if not re.fullmatch(r'[A-Za-z0-9_]+', name): raise Stop('Unsupported SQL identifier')
    return '`' + name + '`'


@contextmanager
def client_config():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(GALERA)
    auth = parser.get('mysqld', 'wsrep_sst_auth', fallback='').split(':', 1)
    if len(auth) != 2 or not auth[0] or not auth[1]: raise Stop('Cannot read the local MariaDB administrative credential')
    def quote(value): return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
    fd, name = tempfile.mkstemp(prefix='ovn-rollback-client-', suffix='.cnf')
    remote = '/tmp/' + Path(name).name
    try:
        with os.fdopen(fd, 'w') as stream: stream.write('[client]\nuser=' + quote(auth[0]) + '\npassword=' + quote(auth[1]) + '\n')
        command(['docker', 'cp', name, 'mariadb:' + remote])
        command(['docker', 'exec', '--user', 'root', 'mariadb', 'chmod', '0600', remote])
        yield ['--defaults-extra-file=' + remote]
    finally:
        Path(name).unlink(missing_ok=True)
        command(['docker', 'exec', '--user', 'root', 'mariadb', 'rm', '-f', remote], check=False)


def query(container, options, sql):
    raw = command(['docker', 'exec', '-i', '--user', 'root', container, 'mariadb'] + options +
                  ['--batch', '--raw', '--skip-column-names', '--default-character-set=utf8mb4'], input="SET time_zone='+00:00';\n"+sql, timeout=120).stdout
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def tables(container, options, database):
    sql_name(database)
    meta = query(container, options,
        "SELECT JSON_OBJECT('table',TABLE_NAME,'column',COLUMN_NAME,'type',DATA_TYPE) FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA='{database}' ORDER BY TABLE_NAME,ORDINAL_POSITION;")
    if not meta: raise Stop('Neutron schema is empty')
    columns = {}
    for row in meta: columns.setdefault(row['table'], []).append(row)
    statements = []
    for table, rows in columns.items():
        fields = []
        for row in rows:
            column = sql_name(row['column'])
            if row['type'] in ('blob', 'tinyblob', 'mediumblob', 'longblob', 'binary', 'varbinary', 'bit'):
                column = 'HEX(' + column + ')'
            fields.extend(["'" + row['column'] + "'", column])
        statements.append("SELECT JSON_OBJECT('table','" + table + "','row',JSON_OBJECT(" + ','.join(fields) +
                          ')) FROM ' + sql_name(database) + '.' + sql_name(table) + ';')
    result = {name: [] for name in columns}
    for row in query(container, options, '\n'.join(statements)): result[row['table']].append(row['row'])
    return result


def canonical(path):
    # Sorting is only for round-trip comparison, never for the restore input.
    lines = sorted(line.rstrip() for line in Path(path).read_text().splitlines() if line.strip())
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def dump(container, options, database, path):
    argv = ['docker', 'exec', '--user', 'root', container, 'mariadb-dump'] + options + DUMP + [database]
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        proc = subprocess.run(argv, stdout=output, stderr=subprocess.PIPE, text=True, timeout=600)
    if proc.returncode or Path(path).stat().st_size < 100: raise Stop('Neutron dump failed or is empty')


def import_dump(container, options, path):
    with Path(path).open('rb') as stream:
        proc = subprocess.run(['docker', 'exec', '-i', '--user', 'root', container, 'mariadb'] + options,
                              stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    if proc.returncode: raise Stop('Neutron SQL import failed; keep APIs and agents stopped')


def shadow_name(run): return 'ovn-rollback-check-' + run


def shadow_options(): return ['--no-defaults', '--socket=/tmp/rollback.sock', '--user=root']


def prepare(directory, cfg):
    database = cfg.get('database', 'neutron'); sql_name(database)
    size = cfg.get('shadow_size', '2g')
    if not re.fullmatch(r'[1-9][0-9]*[mg]', size): raise Stop('Invalid shadow tmpfs size')
    image = command(['docker', 'inspect', 'mariadb', '--format', '{{.Image}}']).stdout.strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise Stop('MariaDB image cannot be pinned')
    with client_config() as options:
        engines = query('mariadb', options, "SELECT JSON_OBJECT('name',TABLE_NAME,'engine',ENGINE) FROM information_schema.TABLES " +
                        f"WHERE TABLE_SCHEMA='{database}';")
        if not engines or any(r['engine'] != 'InnoDB' for r in engines): raise Stop('Checkpoint requires an all-InnoDB Neutron schema')
        special = query('mariadb', options, "SELECT JSON_OBJECT('count',COUNT(*)) FROM information_schema.TRIGGERS " +
                        f"WHERE TRIGGER_SCHEMA='{database}' UNION ALL SELECT JSON_OBJECT('count',COUNT(*)) FROM information_schema.EVENTS " +
                        f"WHERE EVENT_SCHEMA='{database}' UNION ALL SELECT JSON_OBJECT('count',COUNT(*)) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='{database}';")
        if any(r['count'] for r in special): raise Stop('Custom Neutron triggers/events/routines require a separate recovery design')
    name = shadow_name(cfg['run'])
    inspect = command(['docker', 'inspect', name], check=False)
    if inspect.returncode == 0:
        row = json.loads(inspect.stdout)[0]
        if (row['Config'].get('Labels') or {}).get('ovs-to-ovn.rollback-run') != cfg['run'] or row['Image'] != image:
            raise Stop('Shadow MariaDB belongs to a different run/image')
    else:
        command(['docker', 'run', '--detach', '--pull=never', '--network=none', '--user=root', '--name', name,
                 '--label', 'ovs-to-ovn.rollback-run=' + cfg['run'], '--tmpfs', '/tmp:rw,nosuid,size=' + size,
                 '--entrypoint', '/bin/bash', image, '-lc', INIT], timeout=30)
    deadline = time.monotonic() + 90
    while True:
        try:
            query(name, shadow_options(), "SELECT JSON_OBJECT('ready',1);")
            break
        except Stop:
            if time.monotonic() >= deadline: raise Stop('Isolated MariaDB did not become ready before freeze')
            time.sleep(1)
    value = dict(schema=1, status='PASS', run=cfg['run'], database=database, image_id=image, shadow=name)
    path = directory / 'database-prepared.json'
    if path.exists() and json.loads(path.read_text()) != value: raise Stop('Database preparation changed')
    save(path, value)
    return value


def checkpoint(directory, cfg):
    if (directory / 'database-checkpoint.json').exists(): raise Stop('Never overwrite a frozen database checkpoint')
    prepared = json.loads((directory / 'database-prepared.json').read_text())
    if command(['docker','inspect','mariadb','--format','{{.Image}}']).stdout.strip() != prepared['image_id']:
        raise Stop('MariaDB image changed since preparation')
    start = time.monotonic()
    journal(directory / 'events.jsonl', 'database-checkpoint', 'intent')
    database = prepared['database']
    dump_path = directory / 'neutron.sql'
    with client_config() as options:
        frozen = tables('mariadb', options, database)
        dump('mariadb', options, database, dump_path)
        import_dump(prepared['shadow'], shadow_options(), dump_path)
        restored = tables(prepared['shadow'], shadow_options(), database)
        redump = directory / 'neutron-rehearsal.sql'
        dump(prepared['shadow'], shadow_options(), database, redump)
        if canonical(dump_path) != canonical(redump) or business_sha(frozen) != business_sha(restored):
            raise Stop('Isolated restore differs from the frozen Neutron database')
        if business_sha(tables('mariadb', options, database)) != business_sha(frozen):
            raise Stop('Neutron business data changed while the API was frozen')
    save(directory / 'database-business.json', frozen)
    value = dict(schema=1, status='PASS', run=cfg['run'], database=database, dump_sha256=sha(dump_path),
                 business_sha256=business_sha(frozen), roundtrip_sha256=canonical(redump), image_id=prepared['image_id'],
                 elapsed_seconds=round(time.monotonic() - start, 6), created_at=time.time())
    save(directory / 'database-checkpoint.json', value)
    journal(directory / 'events.jsonl', 'database-checkpoint', 'complete', elapsed_seconds=value['elapsed_seconds'])
    command(['docker', 'rm', '--force', prepared['shadow']])
    return value


def inspect(directory, cfg):
    checkpoint = json.loads((directory / 'database-checkpoint.json').read_text())
    if checkpoint['run'] != cfg['run'] or sha(directory / 'neutron.sql') != checkpoint['dump_sha256']:
        raise Stop('Frozen Neutron dump identity/hash differs')
    previous = directory / 'database-inspection.json'
    restore_state = directory / 'database-restore.json'
    if restore_state.exists() and json.loads(restore_state.read_text()).get('status') == 'RESTORING':
        # Preserve the approved pre-restore drift proof after a partial import.
        value = json.loads(previous.read_text())
        if value.get('business_matches') is not True: raise Stop('No approved pre-restore drift proof')
        value['resume_partial_import'] = True
        return value
    with client_config() as options: current = tables('mariadb', options, checkpoint['database'])
    failure_dump=directory/'neutron-before-rollback.sql'
    if not failure_dump.exists():
        with client_config() as options:dump('mariadb',options,checkpoint['database'],failure_dump)
    save(directory / 'database-current.json', current)
    value = dict(status='PASS', run=cfg['run'], business_matches=business_sha(current) == checkpoint['business_sha256'],
                 current_business_sha256=business_sha(current), checkpoint=checkpoint,
                 metadata_port_ids=[r['id'] for r in current.get('ports', []) if r.get('device_owner') == 'network:distributed'])
    value['failure_dump_sha256']=sha(failure_dump)
    save(directory / 'database-inspection.json', value)
    return value


def restore(directory, cfg):
    if cfg.get('execute') is not True or cfg.get('maintenance') is not True or cfg.get('all_writers_stopped') is not True:
        raise Stop('Restore requires execution authorization, enforced maintenance and all Neutron writers stopped')
    checkpoint = json.loads((directory / 'database-checkpoint.json').read_text())
    if command(['docker','inspect','mariadb','--format','{{.Image}}']).stdout.strip() != checkpoint['image_id']:
        raise Stop('MariaDB image differs from the tested checkpoint image')
    if sha(directory / 'neutron.sql') != checkpoint['dump_sha256']: raise Stop('Frozen dump hash changed')
    inspection = json.loads((directory / 'database-inspection.json').read_text())
    if inspection.get('run') != cfg['run'] or inspection.get('business_matches') is not True: raise Stop('Unreconciled Neutron business changes block restore')
    state_path = directory / 'database-restore.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state and (state.get('run') != cfg['run'] or state.get('dump_sha256') != checkpoint['dump_sha256']):
        raise Stop('Partial restore journal belongs to another checkpoint')
    with client_config() as options:
        if state.get('status') != 'RESTORING' and business_sha(tables('mariadb', options, checkpoint['database'])) != inspection['current_business_sha256']:
            raise Stop('Neutron changed after the restore plan was made')
        save(state_path, dict(status='RESTORING', run=cfg['run'], dump_sha256=checkpoint['dump_sha256']))
        journal(directory / 'events.jsonl', 'database-restore', 'intent')
        # --databases dump contains DROP TABLE statements for every original
        # table. Explicit database replacement also removes OVN-added objects.
        query('mariadb', options, 'DROP DATABASE IF EXISTS ' + sql_name(checkpoint['database']) + ';')
        import_dump('mariadb', options, directory / 'neutron.sql')
        verification=directory/'neutron-restored.sql'
        dump('mariadb',options,checkpoint['database'],verification)
        if canonical(verification) != checkpoint['roundtrip_sha256']:
            raise Stop('Restored full schema/data differs from the source; keep APIs and agents stopped')
        if business_sha(tables('mariadb', options, checkpoint['database'])) != checkpoint['business_sha256']:
            raise Stop('Restored Neutron business data differs; do not start services')
    value = dict(status='PASS', run=cfg['run'], dump_sha256=checkpoint['dump_sha256'], restored_at=time.time())
    save(state_path, value); journal(directory / 'events.jsonl', 'database-restore', 'complete')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'checkpoint', 'inspect', 'restore'))
    parser.add_argument('config'); args = parser.parse_args(); cfg = json.loads(args.config)
    try:
        directory = owned(cfg['run'], cfg['host'])
        result = globals()[args.action](directory, cfg)
    except Exception as exc:
        result = dict(status='STOP', error_type=type(exc).__name__)
        if isinstance(exc, Stop): result['reason'] = str(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__': sys.exit(main())
