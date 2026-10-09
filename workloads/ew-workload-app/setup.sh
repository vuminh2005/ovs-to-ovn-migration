#!/usr/bin/env bash
# Run as root inside ew-app after extracting the bundle.
set -euo pipefail
test "$(id -u)" -eq 0
test "$(hostname)" = ew-app
task_src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

/usr/bin/python3 -c 'import gunicorn, psycopg2, pika'
test -r /etc/ew-lab/db-password
test -r /etc/ew-lab/mq-password
for task_file in common.py api.py worker.py probe.py; do
    test -f "$task_src/$task_file"
done

install -d -m 755 /opt/ew-lab
for task_file in common.py api.py worker.py probe.py; do
    install -o root -g root -m 644 "$task_src/$task_file" "/opt/ew-lab/$task_file"
done

/usr/bin/python3 - <<'PY'
import os
import re
from pathlib import Path
os.umask(0o077)
directory = Path('/etc/ew-lab')
passwords = {kind: (directory / (kind + '-password')).read_text().strip()
             for kind in ('db', 'mq')}
if not all(re.fullmatch(r'[0-9a-f]{48}', value) for value in passwords.values()):
    raise SystemExit('Unexpected password format; no credentials printed')
values = [
    'EW_DB_HOST=192.168.102.12', 'EW_MQ_HOST=192.168.102.11',
    'EW_DB_PASSWORD=' + passwords['db'], 'EW_MQ_PASSWORD=' + passwords['mq'],
    'PYTHONUNBUFFERED=1', 'PYTHONDONTWRITEBYTECODE=1',
]
path = directory / 'app.env'
path.write_text('\n'.join(values) + '\n')
path.chmod(0o600)
PY

/usr/bin/python3 -m py_compile /opt/ew-lab/common.py /opt/ew-lab/api.py /opt/ew-lab/worker.py
set -a
source /etc/ew-lab/app.env
set +a
/usr/bin/python3 /opt/ew-lab/common.py

cat > /etc/systemd/system/ew-api.service <<'UNIT'
[Unit]
Description=East-West workload HTTP API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/ew-lab
EnvironmentFile=/etc/ew-lab/app.env
ExecStart=/usr/bin/python3 -m gunicorn --bind 192.168.101.11:8080 --workers 2 --threads 2 --timeout 30 --access-logfile - --error-logfile - api:application
Restart=always
RestartSec=2
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/ew-worker.service <<'UNIT'
[Unit]
Description=East-West outbox publisher and task worker
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/ew-lab
EnvironmentFile=/etc/ew-lab/app.env
ExecStart=/usr/bin/python3 -u /opt/ew-lab/worker.py
Restart=always
RestartSec=2
TimeoutStopSec=15
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable ew-api.service ew-worker.service </dev/null
systemctl restart ew-api.service ew-worker.service </dev/null
systemctl is-active --quiet ew-api.service
systemctl is-active --quiet ew-worker.service

task_ready=false
for task_attempt in $(seq 1 15); do
    if curl --noproxy '*' --connect-timeout 3 --max-time 15 -fsS \
        http://192.168.101.11:8080/health > /tmp/ew-app-health.json; then
        cat /tmp/ew-app-health.json
        echo
        task_ready=true
        break
    fi
    sleep 2
done
if [ "$task_ready" != true ]; then
    journalctl -u ew-api.service -u ew-worker.service -n 60 --no-pager
    exit 1
fi

systemctl is-active --quiet ew-api.service
systemctl is-active --quiet ew-worker.service
ss -lnt '( sport = :8080 )'
echo EW_APP_SERVICES_OK
