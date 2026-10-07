#!/usr/bin/env python3
"""Phase-07 Caracal L3 provider compatibility update, using Neutron sessions.

Neutron 24.2.x migrate mode omits this update. Match newer upstream semantics:
https://github.com/openstack/neutron/blob/master/neutron/plugins/ml2/drivers/ovn/db_migration.py
Snapshot/migrate run only in the configured neutron-server image; verification
runs locally against the fresh post-update snapshot. No plugin is started.
"""
import argparse
import json
import os
import pathlib
import sys


LEGACY_PROVIDERS = ('single_node', 'ha', 'dvr', 'dvrha')
TARGET_PROVIDER = 'ovn'


def snapshot(session, model):
    return [dict(resource_id=row.resource_id, provider_name=row.provider_name)
            for row in session.query(model).order_by(model.resource_id, model.provider_name).all()]


def migrate(session, model):
    """Update existing associations in place; the caller commits the transaction."""
    return session.query(model).filter(model.provider_name.in_(LEGACY_PROVIDERS)).update(
        {'provider_name': TARGET_PROVIDER}, synchronize_session=False)


def verification(rows):
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key]
                                            for key in ('resource_id', 'provider_name')) for row in rows):
        raise ValueError('Malformed provider association snapshot')
    legacy = [row for row in rows if row['provider_name'] in LEGACY_PROVIDERS]
    return dict(status='FAIL' if legacy else 'PASS', legacy_provider_count=len(legacy),
                legacy_providers=legacy)


def verify_snapshot(root):
    try:
        result = verification(json.loads((root/'provider-associations.after.json').read_text()))
    except (OSError, ValueError, TypeError):
        result = dict(status='FAIL', legacy_provider_count=None, legacy_providers=[],
                      reason='Missing or malformed post-migration provider association snapshot')
    dest = root/'provider-associations-verification.json'
    temp = dest.with_suffix('.tmp')
    with open(temp, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
    temp.chmod(0o600)
    temp.replace(dest)
    return result


def neutron_operation(action):
    # Lazy imports allow local verification and unit tests without Neutron.
    from neutron.common import config as common_config
    from oslo_config import cfg
    from oslo_db import options as db_options
    common_config.register_common_config_options()
    db_options.set_defaults(cfg.CONF)
    cfg.CONF(args=['--config-file', '/etc/neutron/neutron.conf',
                   '--config-file', '/etc/neutron/plugins/ml2/ml2_conf.ini'], project='neutron')

    from neutron_lib import context as n_context
    from neutron_lib.db import api as db_api
    from neutron.db.models import servicetype
    ctx = n_context.get_admin_context()
    # Read the primary database too, avoiding replica lag in the AFTER snapshot.
    with db_api.CONTEXT_WRITER.using(ctx) as session:
        if action == 'snapshot':
            result = snapshot(session, servicetype.ProviderResourceAssociation)
        else:
            result = dict(status='PASS', changed_count=migrate(session, servicetype.ProviderResourceAssociation))
    return result  # report success only after the writer transaction commits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'migrate', 'verify'))
    parser.add_argument('--run-dir', type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.action == 'verify' and args.run_dir is None:
        parser.error('verify requires --run-dir')
    try:
        result = verify_snapshot(args.run_dir) if args.action == 'verify' else neutron_operation(args.action)
    except Exception as exc:
        # Database exception messages can contain connection credentials.
        print(f'Provider association {args.action} failed ({type(exc).__name__}); '
              'inspect protected Neutron database logs', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if args.action == 'snapshot' or result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
