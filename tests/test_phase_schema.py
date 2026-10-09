"""Versioned numbering, timing, checkpoint safety and offline report regressions."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from test_validation import ROOT, v
from test_three_pairs import Scenario
import phase_schema as p


class PhaseSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); (self.root/'metrics').mkdir()

    def version(self, version):
        (self.root/'runtime.json').write_text(json.dumps({'phase_marker_schema_version':version}))

    def marker(self, name, value=100):
        (self.root/'metrics'/name).write_text(str(value))

    def report(self):
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(self.root),'test','inventory'],
                       check=True,stdout=subprocess.DEVNULL)
        return json.loads((self.root/'migration-report.json').read_text())

    def test_schema_never_inferred_from_numeric_files(self):
        self.marker('phase12.start'); self.marker('phase12.end',110)
        self.assertEqual(p.schema_version(self.root),1)
        self.assertEqual(p.phase_measurements(self.root)[12]['availability'],'NOT MEASURED')
        self.version(2)
        self.assertEqual(p.phase_measurements(self.root)[12]['duration_seconds'],10)

    def test_unknown_or_malformed_metadata_fails_closed(self):
        for version in (None,True,'2',0,3):
            with self.subTest(version=version):
                self.version(version)
                for check in (p.schema_version,p.resume_eligible,p.pre_cutover_reboot_prohibited):
                    with self.assertRaises(ValueError): check(self.root)
        (self.root/'runtime.json').write_text('[]')
        with self.assertRaises(ValueError): p.schema_version(self.root)

    def test_exact_legacy_mapping_and_missing_phase_equivalents(self):
        expected={0:0,1:1,2:2,5:3,6:4,7:5,8:6,9:7,10:8,11:9,13:10}
        for phase in range(14):
            self.assertEqual(p.marker_name(1,phase,'start'),
                             f'phase{expected[phase]:02d}.start' if phase in expected else None)
            self.assertEqual(p.marker_name(2,phase,'end'),f'phase{phase:02d}.end')
        self.assertIsNone(p.marker_name(1,13,'end'))

    def test_missing_or_invalid_end_is_never_zero_or_measured(self):
        self.version(2); self.marker('phase04.start')
        self.assertEqual(p.phase_measurements(self.root)[4]['availability'],'INCOMPLETE')
        self.assertIsNone(p.phase_measurements(self.root)[4]['duration_seconds'])
        for value in ('nan','inf','bad',99):
            with self.subTest(value=value):
                self.marker('phase04.end',value)
                self.assertEqual(p.phase_measurements(self.root)[4]['availability'],'UNAVAILABLE')
                self.assertIsNone(p.phase_measurements(self.root)[4]['duration_seconds'])

    def test_end_without_start_is_unavailable(self):
        self.version(2); self.marker('phase04.end')
        row=p.phase_measurements(self.root)[4]
        self.assertEqual(row['availability'],'UNAVAILABLE'); self.assertIsNone(row['duration_seconds'])

    def test_resume_reexecution_invalidates_stale_canonical_end(self):
        self.version(2); self.marker('phase09.start'); self.marker('phase09.end',110)
        p.mark(self.root,9,'start',120)
        self.assertFalse((self.root/'metrics/phase09.end').exists())
        self.assertEqual(p.phase_measurements(self.root)[9]['availability'],'INCOMPLETE')
        p.mark(self.root,9,'end',140)
        self.assertEqual(p.phase_measurements(self.root)[9]['duration_seconds'],20)

    def test_legacy_resume_writes_only_legacy_paths_without_upgrading_runtime(self):
        (self.root/'runtime.json').write_text('{"run_id":"old"}')
        original=(self.root/'runtime.json').read_bytes()
        p.mark(self.root,9,'start',120); p.mark(self.root,9,'end',140)
        self.assertEqual((self.root/'metrics/phase07.start').read_text().strip(),'120.000000000')
        self.assertFalse((self.root/'metrics/phase09.start').exists())
        for phase in (3,4,12):
            p.mark(self.root,phase,'start',150); p.mark(self.root,phase,'end',160)
        self.assertEqual(sorted(x.name for x in (self.root/'metrics').iterdir()),['phase07.end','phase07.start'])
        self.assertEqual((self.root/'runtime.json').read_bytes(),original)

    def test_resume_guards_use_schema_specific_takeover_or_cleanup(self):
        for version in (1,2):
            for marker,eligible in (
                (p.marker_name(version,8,'start'),False),
                (p.marker_name(version,8,'end'),True),
                (p.marker_name(version,9,'start'),True),
                (p.marker_name(version,7,'start'),False)):
                with self.subTest(version=version,marker=marker):
                    self.version(version)
                    for path in (self.root/'metrics').iterdir(): path.unlink()
                    self.marker(marker)
                    self.assertEqual(p.resume_eligible(self.root),eligible)

    def test_ambiguous_phase07_start_is_db_only_in_canonical_schema(self):
        self.marker('phase07.start')
        self.assertTrue(p.resume_eligible(self.root))
        self.version(2); self.assertFalse(p.resume_eligible(self.root))

    def test_reboot_guards_distinguish_preparation_from_freeze_and_cutover(self):
        for version in (1,2):
            self.version(version)
            for phase in (5,6,7,8):
                with self.subTest(version=version,phase=phase):
                    for path in (self.root/'metrics').iterdir(): path.unlink()
                    self.marker(p.marker_name(version,phase,'start'))
                    self.assertEqual(p.pre_cutover_reboot_prohibited(self.root),phase in ((7,8) if version==1 else (8,)))
            for path in (self.root/'metrics').iterdir(): path.unlink()
            self.marker('control_plane_downtime.start')
            self.assertTrue(p.pre_cutover_reboot_prohibited(self.root))

    def test_actual_pair_b_guard_keeps_owned_reboot_rules_for_both_schemas(self):
        for version in (1,2):
            for phase in (6,7,8):
                with self.subTest(version=version,phase=phase):
                    self.version(version)
                    for path in (self.root/'metrics').iterdir(): path.unlink()
                    scenario=Scenario(self.root)
                    scenario.obj.cfg['allow_pre_cutover_guest_reboot']=True
                    vm=scenario.obj.pair('pre')['0']
                    entry={key:vm[key] for key in ('server','port','fixed_ips')}
                    self.marker(p.marker_name(version,phase,'start'))
                    if phase==6 or (version==2 and phase==7):
                        scenario.obj.assert_reboot_owner('0',entry)
                    else:
                        with self.assertRaisesRegex(RuntimeError,'prohibited'):
                            scenario.obj.assert_reboot_owner('0',entry)
                    scenario.obj.cloud.compute.reboot_server.assert_not_called()

    def test_phase07_entry_and_freeze_boundary_are_separate_without_changing_legacy_scope(self):
        for version in (1,2):
            with self.subTest(version=version):
                self.version(version)
                for path in (self.root/'metrics').iterdir(): path.unlink()
                with patch.object(sys,'argv',['phase_schema.py','phase-entry',str(self.root),'07']), \
                        patch.object(p.time,'time',return_value=100):
                    p.main()
                self.assertFalse(p.pre_cutover_reboot_prohibited(self.root))
                self.assertEqual(p.phase_measurements(self.root)[7]['availability'],
                                 'INCOMPLETE' if version==2 else 'NOT MEASURED')
                with patch.object(sys,'argv',['phase_schema.py','mark',str(self.root),'07','start','--freeze-start']), \
                        patch.object(p.time,'time',return_value=150):
                    p.main()
                self.assertTrue(p.pre_cutover_reboot_prohibited(self.root))
                self.assertEqual(p.timestamp(self.root/'metrics/control_plane_downtime.start'),150)
                path=self.root/'metrics'/p.marker_name(version,7,'start')
                self.assertEqual(p.timestamp(path),100 if version==2 else 150)

    def test_canonical_db_start_is_also_a_post_freeze_reboot_guard(self):
        self.version(2); self.marker('db_migration.start')
        self.assertTrue(p.pre_cutover_reboot_prohibited(self.root))

    def test_new_total_and_phase13_include_finalization_but_legacy_scope_stays_start_only(self):
        for version in (1,2):
            with self.subTest(version=version):
                self.version(version)
                for path in (self.root/'metrics').iterdir(): path.unlink()
                self.marker('total.start',10)
                with patch.object(p.time,'time',return_value=100): p.report_start(self.root)
                if version==2:
                    self.assertFalse((self.root/'metrics/total.end').exists())
                else:
                    self.assertEqual(p.timestamp(self.root/'metrics/total.end'),100)
                with patch.object(p.time,'time',return_value=130): p.finish(self.root)
                self.assertEqual(p.timestamp(self.root/'metrics/total.end'),130 if version==2 else 100)
                row=p.phase_measurements(self.root)[13]
                self.assertEqual(row['duration_seconds'],30 if version==2 else None)
                self.assertEqual(row['availability'],'MEASURED' if version==2 else 'INCOMPLETE')
                self.assertFalse((self.root/'metrics/phase10.end').exists())

    def test_reports_use_canonical_numbers_with_explicit_legacy_unmeasured_phases(self):
        for old in range(10):
            self.marker(f'phase{old:02d}.start',old*10); self.marker(f'phase{old:02d}.end',old*10+4)
        self.marker('phase10.start',100)
        report=self.report()
        self.assertEqual(report['phase_marker_schema_version'],1)
        self.assertEqual(len(report['phase_timings']),14)
        for number in (3,4,12):
            self.assertEqual(report['phase_timings'][number]['availability'],'NOT MEASURED')
            self.assertIsNone(report['phase_durations_seconds'][f'phase_{number:02d}'])
        self.assertEqual(report['phase_timings'][5]['filename'],'05-stage-ovn-db.yml')
        self.assertEqual(report['phase_timings'][5]['start_marker'],'phase03.start')
        self.assertEqual(report['phase_timings'][5]['duration_seconds'],4)
        text=(self.root/'migration-report.txt').read_text()
        self.assertIn('Phase 03 validation prerequisites (03-validation-prerequisites.yml): NOT MEASURED',text)
        self.assertIn('Phase 13 report/finalization (13-report.yml): INCOMPLETE',text)

    def test_new_reports_cover_all_fourteen_phases_and_keep_dedicated_metrics(self):
        self.version(2)
        for phase in range(14): p.mark(self.root,phase,'start',phase*10); p.mark(self.root,phase,'end',phase*10+5)
        for metric in ('total','db_migration','control_plane_downtime','dataplane_convergence'):
            self.marker(metric+'.start',100); self.marker(metric+'.end',125)
        report=self.report()
        self.assertEqual(report['phase_marker_schema_version'],2)
        self.assertEqual(report['phase_durations_seconds'],{f'phase_{i:02d}':5 for i in range(14)})
        self.assertTrue(all(row['availability']=='MEASURED' for row in report['phase_timings']))
        for field in ('total_duration_seconds','db_migration_seconds','control_plane_downtime_seconds','ovn_portbinding_convergence_seconds'):
            self.assertEqual(report[field],25)
        self.assertIn('capture cleanup',report['total_duration_scope'])
        self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE')

    def test_report_before_finalization_keeps_total_and_phase13_incomplete(self):
        self.version(2); self.marker('total.start',10)
        with patch.object(p.time,'time',return_value=100): p.report_start(self.root)
        report=self.report()
        self.assertIsNone(report['total_duration_seconds'])
        self.assertEqual(report['phase_timings'][13]['availability'],'INCOMPLETE')
        self.assertIsNone(report['phase_durations_seconds']['phase_13'])
        self.assertIn('Total duration: UNAVAILABLE', (self.root/'migration-report.txt').read_text())


class OrchestrationPhaseTests(unittest.TestCase):
    def setUp(self):
        self.imports=yaml.safe_load((ROOT/'migrate-to-ovn.yml').read_text())

    def test_canonical_order_and_all_play_labels_match_filenames(self):
        self.assertEqual([row['import_playbook'] for row in self.imports],['playbooks/'+filename for _,filename in p.PHASES])
        for phase,row in enumerate(self.imports):
            for play in yaml.safe_load((ROOT/row['import_playbook']).read_text()):
                self.assertTrue(play['name'].startswith(f'Phase {phase:02d} - '),play['name'])

    def test_each_file_has_schema_aware_start_and_end_covering_all_plays(self):
        for phase,row in enumerate(self.imports):
            plays=yaml.safe_load((ROOT/row['import_playbook']).read_text())
            tasks=[task for play in plays for task in play['tasks']]
            markers=[(index,task['ansible.builtin.command']['argv']) for index,task in enumerate(tasks)
                     if 'phase_schema.py' in str(task.get('ansible.builtin.command',{}))]
            if phase==7:
                self.assertEqual(markers[0][0],0)
                self.assertEqual(markers[0][1][2],'phase-entry')
                markers=markers[1:]  # legacy freeze boundary + common phase END
            self.assertEqual(len(markers),2,(phase,markers))
            start,end=markers
            if phase==13:
                self.assertEqual(start[1][2],'report-start'); self.assertEqual(end[1][2],'finish')
            else:
                self.assertEqual(start[1][4:6],[f'{phase:02d}','start'])
                self.assertEqual(end[1][4:6],[f'{phase:02d}','end'])
                self.assertEqual(end[0],len(tasks)-1)
            if phase not in (0,7): self.assertEqual(start[0],0)
            self.assertFalse(any('/metrics/phase' in task.get('ansible.builtin.shell','') for task in tasks))

    def test_bootstrap_metadata_precedes_markers_and_includes_initial_discovery_time(self):
        tasks=yaml.safe_load((ROOT/'playbooks/00-bootstrap.yml').read_text())[0]['tasks']
        names=[task['name'] for task in tasks]
        self.assertEqual(names[0],'Timestamp the first bootstrap task')
        self.assertLess(names.index('Save discovered runtime metadata'),names.index('Mark phase 00 start'))
        self.assertIn("'phase_marker_schema_version': 2",tasks[names.index('Save discovered runtime metadata')]['ansible.builtin.copy']['content'])
        self.assertIn('--timestamp',tasks[names.index('Mark phase 00 start')]['ansible.builtin.command']['argv'])

    def test_phase07_timer_covers_readiness_without_earlier_freeze_checkpoint(self):
        tasks=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())[0]['tasks']
        self.assertIn('Mark phase 07 entry',tasks[0]['name'])
        self.assertEqual(tasks[0]['ansible.builtin.command']['argv'][2],'phase-entry')
        self.assertIn('before any Neutron freeze',tasks[1]['name'])
        self.assertTrue(any('--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]) for t in tasks))

    def test_phase13_cleanup_gate_precedes_timing_publication_and_end(self):
        tasks=yaml.safe_load((ROOT/'playbooks/13-report.yml').read_text())[0]['tasks']
        names=[task['name'] for task in tasks]
        before='Rebuild final report including all three pairs cleanup evidence'
        cleanup='Remove compute copy only after successful final reports are persisted'
        end='Mark phase 13 and total end after finalization and capture cleanup'
        publication='Publish final timing metadata without repeating finalization or cleanup'
        self.assertLess(names.index(before),names.index(cleanup))
        self.assertLess(names.index(cleanup),names.index(end))
        self.assertLess(names.index(end),names.index(publication))
        self.assertIn('Return an honest failure',names[-1])
        self.assertEqual(sum('capture-cleanup' in task.get('ansible.builtin.shell','') for task in tasks),1)
        self.assertEqual(sum(' finalize ' in task.get('ansible.builtin.shell','') for task in tasks),1)

    def test_resume_uses_shared_schema_check_and_preserves_live_safety_checks(self):
        plays=yaml.safe_load((ROOT/'playbooks/resume-bootstrap.yml').read_text())
        check=next(task for task in plays[0]['tasks'] if task['name'].startswith('Verify takeover checkpoint'))
        self.assertIn('phase_schema.py',check['ansible.builtin.shell'])
        self.assertIn('resume-check',check['ansible.builtin.shell'])
        self.assertNotIn('/metrics/phase06.end',check['ansible.builtin.shell'])
        self.assertTrue(all(play['any_errors_fatal'] for play in plays))
        live=plays[1]['tasks'][0]['ansible.builtin.shell']
        self.assertIn('docker inspect ovn_controller',live); self.assertIn('systemctl is-active',live)


if __name__=='__main__': unittest.main()
