"""Controller-observed task timing, without module arguments or result content."""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
import time
import uuid

from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r'''
name: migration_task_timing
type: aggregate
short_description: Save per-host task timestamps in the migration run backup
description:
  - Buffers bootstrap events until migration_run_dir is resolved by set_fact.
  - Saves separate JSONL and JSON files for each Ansible invocation.
  - Does not record task arguments, module output, loop items or fact values.
requirements:
  - Enable this callback through the repository ansible.cfg.
'''


def stamp():
    epoch = time.time()
    return dict(epoch=epoch, utc=datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat(timespec='microseconds'),
        monotonic=time.monotonic())


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'aggregate'
    CALLBACK_NAME = 'migration_task_timing'
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.invocation = uuid.uuid4().hex
        self.parent = os.environ.get('OVS_TO_OVN_TASK_TIMING_INVOCATION')
        self.started = stamp()
        self.playbook = None
        self.play = None
        self.root = None
        self.journal = None
        self.buffer = []
        self.definitions = {}
        self.active = {}
        self.rows = []
        self.errors = []
        self._warned = False

    def _warn(self, error):
        # Never include a result, task args or arbitrary exception content.
        self.errors.append(type(error).__name__)
        if not self._warned:
            self._display.warning('Task timing could not be fully persisted; check run/task-timings permissions and free space.')
            self._warned = True

    def _write(self, event):
        event = dict(event, schema_version=1, invocation_id=self.invocation)
        if self.journal is None:
            self.buffer.append(event)
            return
        try:
            with self.journal.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=True) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            self._warn(error)

    def _activate(self, value):
        if self.root is not None or not isinstance(value, str) or '{{' in value:
            return
        root = Path(value)
        if not root.is_absolute():
            return
        try:
            directory = root / 'task-timings'
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            journal = directory / (self.invocation + '.jsonl')
            # UUID + exclusive creation: resumes and nested playbooks never overwrite.
            fd = os.open(str(journal), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            self.root, self.journal = root, journal
            # Local helper/Kolla child processes can load the callback from any cwd.
            plugin_dir = str(Path(__file__).resolve().parent)
            paths = os.environ.get('ANSIBLE_CALLBACK_PLUGINS', '').split(os.pathsep)
            os.environ['ANSIBLE_CALLBACK_PLUGINS'] = os.pathsep.join(
                [plugin_dir] + [p for p in paths if p and p != plugin_dir])
            enabled = os.environ.get('ANSIBLE_CALLBACKS_ENABLED', '').split(',')
            os.environ['ANSIBLE_CALLBACKS_ENABLED'] = ','.join(
                [self.CALLBACK_NAME] + [p for p in enabled if p and p != self.CALLBACK_NAME])
            os.environ['OVS_TO_OVN_TASK_TIMING_DIR'] = str(root)
            os.environ['OVS_TO_OVN_TASK_TIMING_INVOCATION'] = self.invocation
            buffered, self.buffer = self.buffer, []
            for event in buffered:
                self._write(event)
        except OSError as error:
            self._warn(error)

    def v2_playbook_on_start(self, playbook):
        self.playbook = str(playbook._file_name)
        self._write(dict(event='invocation_start', timestamp=self.started,
                         playbook=self.playbook, parent_invocation_id=self.parent))
        self._activate(os.environ.get('OVS_TO_OVN_TASK_TIMING_DIR'))

    def v2_playbook_on_play_start(self, play):
        self.play = str(play.name or '')
        # Direct -e migration_run_dir also works without a bootstrap set_fact.
        variables = play.get_variable_manager().get_vars(play=play)
        self._activate(variables.get('migration_run_dir'))

    def _task(self, task, handler=False):
        key = str(task._uuid)
        if key not in self.definitions:
            # Do not use get_name(): unnamed shell tasks can expose their command.
            hidden = task.no_log is not None and task.no_log is not False
            self.definitions[key] = dict(task_uuid=key,
                task_name='[no_log task]' if hidden else str(task.name or '[unnamed task]'),
                action=str(task.action), source=task.get_path(), play=self.play,
                handler=handler, dispatched_at=stamp())
            self._write(dict(event='task_dispatch', **self.definitions[key]))
        return self.definitions[key]

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._task(task)

    def v2_playbook_on_handler_task_start(self, task):
        self._task(task, handler=True)

    def v2_runner_on_start(self, host, task):
        definition = self._task(task)
        row = dict(definition, execution_id=uuid.uuid4().hex, host=host.get_name(),
                   started_at=stamp(), ended_at=None, duration_seconds=None,
                   status='INCOMPLETE', changed=None)
        self.active[(host.get_name(), str(task._uuid))] = row
        self.rows.append(row)
        self._write(dict(event='task_start', **row))

    def _finish(self, result, status, ignore_errors=False):
        # Only this allowlisted fact is used for routing, never serialized.
        facts = result._result.get('ansible_facts', {})
        if isinstance(facts, dict):
            self._activate(facts.get('migration_run_dir'))
        host = result._host.get_name()
        row = self.active.pop((host, str(result._task._uuid)), None)
        if row is None:
            row = dict(self._task(result._task), execution_id=uuid.uuid4().hex,
                       host=host, started_at=None)
            self.rows.append(row)
        ended = stamp()
        row.update(ended_at=ended, duration_seconds=(ended['monotonic'] - row['started_at']['monotonic'])
                   if row['started_at'] else None, status=status,
                   changed=bool(result._result.get('changed', False)),
                   ignored=bool(ignore_errors))
        # Keep only the delegate hostname, never its vars/credentials.
        delegate = result._result.get('_ansible_delegated_vars') or {}
        if isinstance(delegate, dict) and delegate.get('ansible_delegated_host'):
            row['delegated_to'] = str(delegate['ansible_delegated_host'])
        elif isinstance(result._task.delegate_to, str) and '{{' not in result._task.delegate_to:
            row['delegated_to'] = result._task.delegate_to
        self._write(dict(event='task_end', **row))

    def v2_runner_on_ok(self, result):
        self._finish(result, 'OK')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._finish(result, 'FAILED', ignore_errors)

    def v2_runner_on_skipped(self, result):
        self._finish(result, 'SKIPPED')

    def v2_runner_on_unreachable(self, result):
        self._finish(result, 'UNREACHABLE')

    def _collect_failed_run_diagnostics(self, stats):
        workflows = {'migrate-to-ovn.yml', 'resume-before-target.yml', 'resume-after-db.yml',
            'resume-after-cleanup.yml', 'retry-validation-preparation.yml', 'retry-ns-diagnostics-preparation.yml'}
        if (self.root is None or Path(self.playbook or '').name not in workflows or
                os.environ.get('OVS_TO_OVN_DIAGNOSTICS_FINALIZING') == '1' or
                not (self.root/'diagnostics-config.json').exists()):
            return
        failed = any(stats.summarize(host).get('failures', 0) or
            stats.summarize(host).get('unreachable', 0) for host in getattr(stats, 'processed', {}))
        if not failed:
            return
        helper = Path(__file__).resolve().parents[1]/'scripts/diagnostics_control.py'
        env = dict(os.environ, OVS_TO_OVN_DIAGNOSTICS_FINALIZING='1')
        try:
            with (self.root/'diagnostics-abort.log').open('ab') as log:
                subprocess.run([sys.executable, str(helper), 'abort', str(self.root)],
                    stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, timeout=240)
        except (OSError, subprocess.TimeoutExpired):
            self._display.warning('Run failed; diagnostics archive may be incomplete. Inspect diagnostics-abort.log.')

    def v2_playbook_on_stats(self, stats):
        ended = stamp()
        self._write(dict(event='invocation_end', timestamp=ended))
        if self.journal is None:
            return
        payload = dict(schema_version=1, invocation_id=self.invocation,
            parent_invocation_id=self.parent, playbook=self.playbook,
            started_at=self.started, ended_at=ended,
            duration_seconds=ended['monotonic'] - self.started['monotonic'],
            scope='Controller-observed per-host task execution; includes Ansible/transport overhead. Parallel tasks overlap; do not sum durations.',
            persistence_errors=self.errors, tasks=self.rows,
            task_dispatches=list(self.definitions.values()))
        destination = self.journal.with_suffix('.json')
        temporary = destination.with_suffix('.json.tmp')
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError as error:
            self._warn(error)
        self._collect_failed_run_diagnostics(stats)
