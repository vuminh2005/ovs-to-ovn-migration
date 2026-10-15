"""Full OVSDB port/server identity, XML cross-check and capture-resume guards."""
import copy
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_validation import v
import dataplane_capture as c

SERVER='c5d63148-95fd-4ce7-a6a5-83eca57e500d'
PORT='669f8ca1-3421-45a6-9f28-51ecb3d661b8'
TAP='tap669f8ca1-34'


class TapResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)
        self.cfg=dict(server=SERVER,port=PORT,compute_host='selected-compute',run='run',
                      source_ip='10.0.0.2',peer_ip='10.0.1.2',interval=.2)
        self.rows=[self.row()]; self.bridge='br-int'
        self.xml='<domain><uuid>'+SERVER+'</uuid><devices><interface><target dev="'+TAP+'"/></interface></devices></domain>'
        self.present=True; self.detached=False
        self.command=patch.object(c.subprocess,'check_output',side_effect=self.output).start()
        self.exists_patch=patch.object(c.pathlib.Path,'exists',side_effect=lambda:self.present)
        self.exists=self.exists_patch.start()
        self.addCleanup(patch.stopall)

    def row(self, port=PORT, server=SERVER, name=TAP, status='active'):
        external=[['iface-id',port],['vm-uuid',server]]
        if status is not None: external.append(['iface-status',status])
        return [name,['map',external]]

    def output(self, argv, **kwargs):
        if argv[0]=='ovs-vsctl':
            if 'find' in argv:
                self.assertEqual(argv[-1],'external_ids:iface-id='+json.dumps(PORT))
                self.assertIn('--format=json',argv)
                return json.dumps({'headings':['name','external_ids'],'data':self.rows})
            self.assertEqual(argv,['ovs-vsctl','--timeout=10','iface-to-br',TAP])
            if self.detached: raise subprocess.CalledProcessError(1,argv,'no port named interface')
            return self.bridge+'\n'
        self.assertEqual(argv,['docker','exec','nova_libvirt','virsh','dumpxml',SERVER])
        return self.xml

    def exact_xml(self, target=TAP, server=SERVER):
        return ('<domain><uuid>'+server+'</uuid><devices><interface><target dev="'+target+'"/>'
                '<virtualport><parameters interfaceid="'+PORT+'"/></virtualport></interface></devices></domain>')

    def test_exact_full_ovsdb_identity_existing_interface_passes(self):
        self.assertEqual(c.resolve_tap(self.cfg),TAP)
        self.assertEqual(self.command.call_args_list[0].args[0][0],'ovs-vsctl')

    def test_zero_exact_matches_fails_before_xml(self):
        self.rows=[]
        with self.assertRaisesRegex(RuntimeError,'exactly one'): c.resolve_tap(self.cfg)
        self.assertEqual(self.command.call_count,1)

    def test_duplicate_exact_matches_fail(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        with self.assertRaisesRegex(RuntimeError,'exactly one'): c.resolve_tap(self.cfg)

    def test_uuid_prefix_and_resembling_tap_name_are_not_ownership(self):
        self.rows=[self.row(port=PORT[:11])]
        with self.assertRaisesRegex(RuntimeError,'UUID identity'): c.resolve_tap(self.cfg)

    def test_wrong_server_uuid_fails(self):
        self.rows=[self.row(server='another-server')]
        with self.assertRaisesRegex(RuntimeError,'UUID identity'): c.resolve_tap(self.cfg)

    def test_missing_linux_interface_fails(self):
        self.present=False
        with self.assertRaisesRegex(RuntimeError,'does not exist'): c.resolve_tap(self.cfg)

    def test_interface_not_represented_on_ovs_bridge_fails(self):
        self.detached=True
        with self.assertRaises(subprocess.CalledProcessError): c.resolve_tap(self.cfg)

    def test_wrong_integration_bridge_fails(self):
        self.bridge='br-ex'
        with self.assertRaisesRegex(RuntimeError,'integration bridge'): c.resolve_tap(self.cfg)

    def test_missing_xml_port_annotation_with_exact_ovsdb_identity_passes(self):
        self.assertEqual(c.resolve_tap(self.cfg),TAP)

    def test_exact_xml_port_without_tap_target_does_not_replace_ovsdb_authority(self):
        self.xml=self.exact_xml().replace('<target dev="'+TAP+'"/>','')
        self.assertEqual(c.resolve_tap(self.cfg),TAP)

    def test_matching_exact_xml_port_and_tap_passes(self):
        self.xml=self.exact_xml()
        self.assertEqual(c.resolve_tap(self.cfg),TAP)

    def test_conflicting_exact_xml_tap_fails(self):
        self.xml=self.exact_xml(target='tapDifferent')
        with self.assertRaisesRegex(RuntimeError,'conflicts with OVSDB'): c.resolve_tap(self.cfg)

    def test_conflicting_domain_uuid_fails_even_without_port_annotations(self):
        self.xml=self.xml.replace(SERVER,'another-server')
        with self.assertRaisesRegex(RuntimeError,'domain UUID'): c.resolve_tap(self.cfg)

    def test_duplicate_exact_xml_interfaces_fail(self):
        self.xml=self.exact_xml().replace('</devices>',self.exact_xml().split('<devices>')[1].split('</devices>')[0]+'</devices>')
        with self.assertRaisesRegex(RuntimeError,'one libvirt tap'): c.resolve_tap(self.cfg)

    def test_non_active_interface_fails_but_absent_status_is_allowed(self):
        self.rows=[self.row(status='inactive')]
        with self.assertRaisesRegex(RuntimeError,'not active'): c.resolve_tap(self.cfg)
        self.rows=[self.row(status=None)]
        self.assertEqual(c.resolve_tap(self.cfg),TAP)

    def test_unsafe_or_empty_interface_name_fails(self):
        for name in ('','../tap','..','.','tap with space','x'*16):
            with self.subTest(name=name):
                self.rows=[self.row(name=name)]
                with self.assertRaisesRegex(RuntimeError,'unsafe/empty'): c.resolve_tap(self.cfg)

    def test_saved_tap_cannot_be_silently_replaced(self):
        self.rows=[self.row(name='tapDifferent')]
        with self.assertRaisesRegex(RuntimeError,'replacement prohibited'): c.verify_saved_tap(self.cfg,TAP)

    def persist_running(self):
        # Stop mocking filesystem existence so controller/agent journals are real.
        self.exists_patch.stop()
        state=dict(config=self.cfg,tap=TAP,status='RUNNING',supervisor={'pid':1},tcpdump={'pid':2})
        c.save(self.root/'capture-state.json',state)
        return state

    def test_resume_unchanged_exact_identity_reuses_process_without_xml(self):
        state=self.persist_running()
        with patch.object(c.pathlib.Path,'exists',return_value=True),patch.object(c,'alive',return_value=True),patch.object(c.subprocess,'Popen') as launch:
            self.assertEqual(c.agent_start(self.root,self.cfg),state)
            launch.assert_not_called()
        self.assertTrue(all(call.args[0][0]=='ovs-vsctl' for call in self.command.call_args_list))

    def test_resume_changed_port_or_server_uuid_fails_without_new_capture(self):
        for changed in ('port','server'):
            with self.subTest(changed=changed):
                state=self.persist_running()
                self.rows=[self.row(port='another-port')] if changed=='port' else [self.row(server='another-server')]
                with patch.object(c.pathlib.Path,'exists',return_value=True),patch.object(c,'alive',return_value=True),patch.object(c.subprocess,'Popen') as launch:
                    with self.assertRaisesRegex(RuntimeError,'UUID identity'): c.agent_start(self.root,self.cfg)
                    launch.assert_not_called()
                self.assertEqual(json.loads((self.root/'capture-state.json').read_text()),state)

    def test_resume_missing_tap_fails_without_new_capture(self):
        self.persist_running()
        # Only the real state file exists; mock sysfs absence independently.
        original_exists=pathlib.Path.exists
        def exists(path):
            return False if str(path).startswith('/sys/class/net/') else original_exists(path)
        with patch.object(c.pathlib.Path,'exists',exists),patch.object(c,'alive',return_value=True),patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'does not exist'): c.agent_start(self.root,self.cfg)
            launch.assert_not_called()

    def test_controller_saved_tap_conflicting_with_compute_journal_fails(self):
        self.persist_running()
        with patch.object(c,'alive',return_value=True),patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'saved tap conflict'): c.agent_start(self.root,dict(self.cfg,tap='tapDifferent'))
            launch.assert_not_called()

    def test_supervisor_checks_exact_saved_tap_before_tcpdump_launch(self):
        self.exists_patch.stop()
        c.save(self.root/'capture-state.json',dict(config=self.cfg,tap=TAP,status='START_INTENT'))
        with patch.object(c,'verify_saved_tap',side_effect=RuntimeError('changed identity')),patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'changed identity'): c.supervise(self.root)
            launch.assert_not_called()
        self.assertEqual(v.read_evidence(self.root,'capture-state.json')['status'],'FAILED')

    def test_running_snapshot_identity_conflict_preserves_pcap_and_does_not_stop(self):
        state=self.persist_running()
        (self.root/'measure0.pcap').write_bytes(b'raw evidence')
        self.rows=[self.row(server='another-server')]
        with patch.object(c.pathlib.Path,'exists',return_value=True),patch.object(c,'alive',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'UUID identity'): c.agent_snapshot(self.root,stop=True)
        self.assertFalse((self.root/'stop-request').exists())
        self.assertEqual((self.root/'measure0.pcap').read_bytes(),b'raw evidence')
        self.assertEqual(v.read_evidence(self.root,'capture-state.json'),state)

    def test_controller_never_overwrites_saved_tap_with_conflicting_resume_response(self):
        self.exists_patch.stop()
        from types import SimpleNamespace as NS
        capture=c.Capture(NS(root=self.root,cfg={}))
        checkpoint=dict(self.cfg,tap=TAP,status='RUNNING')
        c.save(capture.path,checkpoint)
        capture.transport=Mock(return_value={'status':'RUNNING','tap':'tapDifferent'})
        with self.assertRaisesRegex(RuntimeError,'saved controller tap'): capture.start()
        self.assertEqual(capture.transport.call_count,1)
        self.assertEqual(capture.checkpoint(),checkpoint)


if __name__=='__main__': unittest.main()
