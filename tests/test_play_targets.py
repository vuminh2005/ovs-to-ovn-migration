"""Regression guards for parse-time targets and task-time OVN delegation."""
import pathlib
import unittest
import yaml

ROOT = pathlib.Path(__file__).parents[1]


class PlayTargetTests(unittest.TestCase):
    def test_all_play_targets_are_static(self):
        for path in ROOT.rglob('*.yml'):
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, list):
                continue
            for play in data:
                if isinstance(play, dict) and 'hosts' in play:
                    with self.subTest(path=path, play=play.get('name')):
                        target = play['hosts']
                        self.assertIsInstance(target, str)
                        self.assertNotIn('{{', target)
                        self.assertNotIn('hostvars', target)

    def test_selected_ovn_host_is_task_delegation_only(self):
        count = 0
        for name in ('05-stage-ovn-db.yml', '07-migrate-db.yml',
                     '08-cutover.yml', '12-workload-validation.yml'):
            for play in yaml.safe_load((ROOT/'playbooks'/name).read_text()):
                for task in play.get('tasks', []):
                    if task.get('delegate_to') == '{{ ovn_cli_host_runtime }}':
                        count += 1
                        self.assertEqual(play['hosts'], 'localhost')
                        self.assertTrue(play['become'])
                    if play['hosts'] == 'localhost' and ('ansible.builtin.set_fact' in task or
                            task.get('name', '').startswith(('Persist ', 'Mark '))):
                        self.assertNotIn('delegate_to', task)
        self.assertEqual(count, 8)


if __name__ == '__main__':
    unittest.main()
