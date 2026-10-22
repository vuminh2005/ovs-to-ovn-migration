"""Keep forensic coverage separate from verified collector shutdown and traffic."""


def collection_ready(data):
    if data.get('status') == 'DISABLED':
        return True
    if 'collection_status' in data:
        return data['collection_status'] == 'STOPPED'
    return data.get('status') == 'PASS'  # Previously complete legacy archive.


def coverage(data):
    if data.get('status') == 'DISABLED':
        return dict(status='DISABLED', warnings=[])
    warnings = []
    for collector in data.get('collectors', []):
        label = collector['host']+'/'+collector['role']
        details = []
        for name, job in collector.get('jobs', {}).items():
            gaps = job.get('gaps', [])
            if gaps:
                seconds = sum(g.get('duration_seconds') or 0 for g in gaps)
                details.append(f'{name}: {len(gaps)} gap(s), {seconds:.3f} s recorded')
        if collector.get('errors'):
            details.append('recorded errors: '+', '.join(collector['errors']))
        if details or collector.get('status') != 'PASS':
            warnings.append(label+': '+('; '.join(details) or 'coverage unavailable'))
    if data.get('status') == 'PASS':
        status = 'PASS'
    elif any(j.get('gaps') for c in data.get('collectors', []) for j in c.get('jobs', {}).values()):
        status = 'GAPPED'
    else:
        status = 'UNAVAILABLE'
    if not warnings and status != 'PASS':
        warnings.append('Observer evidence unavailable; inspect migration-diagnostics.json')
    return dict(status=status, collection_status=data.get('collection_status'), warnings=warnings,
                scope='Forensic observer coverage; independent from certified traffic metrics')
