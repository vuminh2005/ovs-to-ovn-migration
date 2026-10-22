"""Record real volume/file locations without copying database backup contents."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
from tests.validation.test_validation import ROOT
from run_layout import initialize, run_path


class BackupLocationsTests(unittest.TestCase):
    def test_actual_metadata_tasks_record_locations_and_honor_disabled_backup(self):
        source=yaml.safe_load((ROOT/'playbooks/01-backup.yml').read_text())
        wanted=('[01] Locate MariaDB backup volumes','[01] [SAVE] Save MariaDB backup locations')
        tasks=[t for p in source for t in p.get('tasks',[]) if t.get('name') in wanted]
        self.assertEqual(len(tasks),2)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); volume=root/'volume'; volume.mkdir()
            original=b'original database backup bytes'
            (volume/'backup.qp').write_bytes(original)
            binary=root/'bin'; binary.mkdir(); calls=root/'docker-called'
            docker=binary/'docker'
            docker.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\n'
                'assert sys.argv[1:]==["volume","inspect","mariadb_backup"]\n'
                'Path('+repr(str(calls))+').write_text("inspect")\n'
                'print(json.dumps([{"Mountpoint":'+repr(str(volume))+'}]))\n')
            docker.chmod(0o700)
            inventory=root/'inventory.ini'
            inventory.write_text('[control]\ndbhost ansible_connection=local '
                'ansible_python_interpreter='+sys.executable+'\n')
            config=root/'isolated.cfg';config.write_text('[defaults]\n')
            env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],
                ANSIBLE_CONFIG=str(config),ANSIBLE_FILTER_PLUGINS=str(ROOT/'plugins/filter'))
            for enabled in (True,False):
                with self.subTest(enabled=enabled):
                    if calls.exists():calls.unlink()
                    run=root/str(enabled);initialize(run)
                    play=root/'metadata.yml'
                    play.write_text(yaml.safe_dump([dict(hosts='localhost',connection='local',gather_facts=False,
                        vars=dict(migration_run_dir=str(run),run_mariadb_backup=enabled),tasks=tasks)],sort_keys=False))
                    proc=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(play)],
                        env=env,capture_output=True,text=True,timeout=30)
                    self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                    result=json.loads(run_path(run,'mariadb-backup.json').read_text())
                    self.assertEqual(result['enabled'],enabled)
                    self.assertEqual(calls.exists(),enabled)
                    if enabled:
                        location=result['locations'][0]
                        self.assertEqual(location['mountpoint'],str(volume))
                        self.assertEqual(location['volume'],'mariadb_backup')
                        self.assertEqual(location['files'][0]['name'],'backup.qp')
                        self.assertEqual(location['files'][0]['size_bytes'],len(original))
                    else:self.assertEqual(result['locations'],[])
                    self.assertEqual((volume/'backup.qp').read_bytes(),original)
                    self.assertFalse(list(run.rglob('*.qp')))


if __name__=='__main__':unittest.main()
