"""Synthetic Ethernet PCAP and capture-process journal regressions (no cloud)."""
import json
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from test_validation import v, ROOT
import dataplane_capture as c


def frame(source, peer, seq, reply=False, ident=123):
    payload=struct.pack('!Q',seq)+b'x'*48
    icmp=struct.pack('!BBHHH',0 if reply else 8,0,0,ident,seq%65536)+payload
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(icmp),0,0,64,1,0,
                   socket.inet_aton(peer if reply else source),socket.inet_aton(source if reply else peer))
    return b'\0'*12+b'\x08\x00'+ip+icmp


def write_pcap(path, count=12, failed=(), gap=None, initial=1):
    packets=[]
    for index in range(1,count+1):
        ts=10+index*.2+(2 if gap and index>=gap else 0)
        seq=initial+index-1
        packets.append((ts,frame('10.0.0.2','10.0.1.2',seq)))
        if index not in failed:
            packets.append((ts+.01,frame('10.0.0.2','10.0.1.2',seq,True)))
    data=b'\xd4\xc3\xb2\xa1'+struct.pack('<HHIIII',2,4,0,0,65535,1)
    for ts,packet in sorted(packets):
        sec=int(ts); frac=round((ts-sec)*1e6)
        data+=struct.pack('<IIII',sec,frac,len(packet),len(packet))+packet
    path.write_bytes(data)


class PcapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); self.path=self.root/'measure0.pcap'
        self.cp=dict(source_ip='10.0.0.2',peer_ip='10.0.1.2',remote=dict(status='STOPPED',returncode=0,
            dropped_packets=0,supervisor_gap=False,started_at=9,stopped_at=100))

    def metrics(self, **kwargs):
        write_pcap(self.path,**kwargs)
        rows,_=c.echo_rows(self.path,self.cp['source_ip'],self.cp['peer_ip'])
        def anchor(row):
            return dict(index=row['index'],timestamp=row['ts'],reply_timestamp=row['reply_timestamp'],identifier=row['identifier'],icmp_sequence=row['icmp_sequence'])
        self.window=dict(pcap_start=anchor(rows[0]),pcap_end=anchor(rows[-1]))
        return c.pcap_metrics(self.path,self.cp,self.window,.2)

    def test_request_reply_success(self):
        result=self.metrics()
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['packets_attempted'],11)
        self.assertEqual(result['packets_successful'],11); self.assertEqual(result['packet_loss_percent'],0)

    def test_no_reply_is_failed_and_longest_recovered_burst_uses_reply_time(self):
        result=self.metrics(failed=(2,3,7))
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['packets_failed'],3)
        self.assertEqual(result['failure_burst_count'],2)
        self.assertEqual(result['maximum_consecutive_failed_probes'],2)
        self.assertAlmostEqual(result['actual_dataplane_outage_seconds'],.41)
        self.assertAlmostEqual(result['packet_loss_percent'],300/11)

    def test_final_unrecovered_burst_is_unavailable(self):
        result=self.metrics(failed=(11,))  # endpoint request12 is recovered, so loss11 recovered
        self.assertEqual(result['status'],'PASS')
        write_pcap(self.path,failed=(11,12))
        result=c.pcap_metrics(self.path,self.cp,self.window,.2)
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertIsNone(result['packet_loss_percent']); self.assertIsNone(result['actual_dataplane_outage_seconds'])

    def test_interior_capture_gap_is_unavailable(self):
        result=self.metrics(gap=5)
        self.assertEqual(result['status'],'UNAVAILABLE'); self.assertIn('gap',result['reason'])
        self.assertIsNone(result['packet_loss_percent'])

    def test_capture_drops_lifetime_gap_or_failed_process_invalidate(self):
        self.metrics()
        for changes in ({'dropped_packets':1},{'supervisor_gap':True},{'status':'RUNNING'},
                        {'started_at':11},{'stopped_at':11},{'returncode':1}):
            with self.subTest(changes=changes):
                cp=dict(self.cp,remote=dict(self.cp['remote'],**changes))
                self.assertEqual(c.pcap_metrics(self.path,cp,self.window,.2)['status'],'UNAVAILABLE')

    def test_identifier_sequence_rollover_is_valid(self):
        self.assertEqual(self.metrics(initial=65530)['status'],'PASS')

    def test_sequence_reset_or_missing_request_is_invalid(self):
        self.metrics()
        data=self.path.read_bytes(); first=c.frames(self.path)
        packets=list(first); corrupted=[]
        for ts,f in packets:
            if int.from_bytes(f[40:42],'big')==6:
                f=f[:40]+b'\0\x01'+f[42:]
            corrupted.append((ts,f))
        out=data[:24]
        for ts,f in corrupted:
            sec=int(ts); out+=struct.pack('<IIII',sec,round((ts-sec)*1e6),len(f),len(f))+f
        self.path.write_bytes(out)
        result=c.pcap_metrics(self.path,self.cp,self.window,.2)
        self.assertEqual(result['status'],'UNAVAILABLE')

    def test_measurement_end_requires_five_recovered_requests_not_health(self):
        write_pcap(self.path,failed=(2,3))
        rows,_=c.echo_rows(self.path,self.cp['source_ip'],self.cp['peer_ip'])
        endpoint=c.recovered_endpoint(rows,.2)
        self.assertEqual(endpoint['index'],12)
        self.assertIsNone(c.recovered_endpoint(rows[:4],.2))
        write_pcap(self.path,failed=(12,)); rows,_=c.echo_rows(self.path,self.cp['source_ip'],self.cp['peer_ip'])
        self.assertIsNone(c.recovered_endpoint(rows,.2))

    def test_console_truncation_and_pair_b_reboot_do_not_affect_pcap(self):
        result=self.metrics(failed=(2,))
        v.save(self.root/'measure0-console-records.json',[])
        v.save(self.root/'existing0-console-records.json',[{'boot':'reboot','success':False}])
        self.assertEqual(c.pcap_metrics(self.path,self.cp,self.window,.2),result)
        self.assertEqual(result['packets_failed'],1)

    def test_raw_pcap_is_preserved_for_invalid_coverage(self):
        self.metrics(gap=5); before=self.path.read_bytes()
        self.assertTrue(self.path.exists()); self.assertEqual(self.path.read_bytes(),before)

    def test_truncated_final_capture_fails_closed(self):
        self.metrics(); self.path.write_bytes(self.path.read_bytes()[:-2])
        self.assertEqual(c.pcap_metrics(self.path,self.cp,self.window,.2)['status'],'UNAVAILABLE')

    def test_endpoint_reply_timestamp_must_match_raw_pcap(self):
        self.metrics()
        self.window['pcap_end']['reply_timestamp']-=1
        self.assertEqual(c.pcap_metrics(self.path,self.cp,self.window,.2)['status'],'UNAVAILABLE')



class CaptureJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name)
        self.cfg=dict(run='run',server='server',port='port',source_ip='10.0.0.2',peer_ip='10.0.1.2',interval=.2)

    def test_tap_derived_only_from_exact_uuid_and_domain(self):
        xml='<domain><uuid>server</uuid><devices><interface><target dev="tapABC"/><virtualport><parameters interfaceid="port"/></virtualport></interface></devices></domain>'
        self.assertEqual(c.tap_from_xml(xml,'server','port'),'tapABC')
        for server,port in [('other','port'),('server','other')]:
            with self.assertRaises(RuntimeError): c.tap_from_xml(xml,server,port)

    def test_running_capture_reused_without_launch_or_api(self):
        state=dict(config=self.cfg,tap='tapABC',status='RUNNING',supervisor={'pid':1},tcpdump={'pid':2})
        c.save(self.root/'capture-state.json',state)
        with patch.object(c,'alive',return_value=True),patch.object(c.subprocess,'Popen') as launch,patch.object(c,'resolve_tap') as tap,patch.object(c,'verify_saved_tap') as verify:
            self.assertEqual(c.agent_start(self.root,self.cfg),state)
            verify.assert_called_once_with(self.cfg,'tapABC')
            launch.assert_not_called(); tap.assert_not_called()

    def test_ambiguous_or_dead_capture_is_never_replaced(self):
        for status in ('START_INTENT','STOPPED','FAILED','RUNNING'):
            c.save(self.root/'capture-state.json',dict(config=self.cfg,status=status))
            with patch.object(c,'alive',return_value=False),patch.object(c.subprocess,'Popen') as launch:
                with self.assertRaises(RuntimeError): c.agent_start(self.root,self.cfg)
                launch.assert_not_called()

    def test_start_intent_precedes_process_launch(self):
        def launch(*args,**kw):
            state=json.loads((self.root/'capture-state.json').read_text())
            self.assertEqual(state['status'],'START_INTENT'); self.assertEqual(state['config']['port'],'port')
            state.update(status='RUNNING',supervisor={'pid':1},tcpdump={'pid':2}); c.save(self.root/'capture-state.json',state)
        with patch.object(c,'resolve_tap',return_value='tapABC'),patch.object(c.subprocess,'Popen',side_effect=launch):
            self.assertEqual(c.agent_start(self.root,self.cfg)['status'],'RUNNING')

    def test_live_capture_snapshot_does_not_use_neutron_or_console(self):
        state=dict(config=self.cfg,status='RUNNING',supervisor={'pid':1},tcpdump={'pid':2},path=str(self.root/'measure0.pcap'),heartbeat_at=12.6)
        write_pcap(self.root/'measure0.pcap'); c.save(self.root/'capture-state.json',state)
        with patch.object(c,'alive',return_value=True),patch.object(c.time,'time',return_value=12.6),patch.object(c,'verify_saved_tap'):
            snapshot=c.agent_snapshot(self.root)
        self.assertEqual(snapshot['status'],'RUNNING')
        self.assertIsNotNone(snapshot['observed_endpoint'])

    def test_controller_resume_uses_checkpoint_without_cloud_api(self):
        obj=NS(root=self.root,cfg={},cloud=Mock(side_effect=RuntimeError('Neutron outage')))
        capture=c.Capture(obj); cp=dict(self.cfg,status='RUNNING',compute_host='compute',directory='/var/lib/owned/run')
        c.save(capture.path,cp); capture.transport=Mock(return_value=dict(status='RUNNING',tap='tapABC'))
        capture.start(); self.assertEqual(capture.transport.call_args.args[1]['port'],'port')
        obj.cloud.assert_not_called()

    def test_start_anchor_requires_running_capture_and_observed_replies(self):
        obj=NS(root=self.root,cfg={'timeout':1}); capture=c.Capture(obj)
        endpoint=dict(index=5,timestamp=11,reply_timestamp=11.01,identifier=123,icmp_sequence=5)
        capture.snapshot=Mock(return_value={'observed_endpoint':endpoint,'parse_problems':[]})
        capture.anchor(); first=v.read_evidence(self.root,'validation-window.json')
        capture.snapshot.side_effect=AssertionError('resume must not reset anchor')
        capture.anchor(); self.assertEqual(v.read_evidence(self.root,'validation-window.json'),first)

    def test_capture_precedes_pair_b_creation_and_staging(self):
        text=(ROOT/'scripts/workload_validation.py').read_text()
        pre=text.split("elif args.action == 'pre':",1)[1].split('    else:',1)[0]
        self.assertLess(pre.index('checkpoint_start'),pre.index("create('pre')"))
        import yaml
        phases=yaml.safe_load((ROOT/'migrate-to-ovn.yml').read_text())
        files=[p['import_playbook'] for p in phases]
        self.assertLess(files.index('playbooks/04-validation-workloads.yml'),files.index('playbooks/05-stage-ovn-db.yml'))

    def test_controller_intent_precedes_launch_and_resumed_capture_avoids_cloud(self):
        fixed=[{'subnet_id':'subnet','ip_address':'10.0.0.2'}]
        pair={'0':dict(server='server',port='port',ip='10.0.0.2',fixed_ips=fixed,owned=True),
              '1':dict(server='peer-server',port='peer-port',ip='10.0.1.2',owned=True)}
        cloud=NS(compute=Mock(),network=Mock())
        cloud.compute.get_server.return_value=NS(id='server',status='ACTIVE',metadata={'ovn_migration_run':'run','ovn_validation_role':'measure'})
        cloud.network.get_port.return_value=NS(id='port',device_id='server',fixed_ips=fixed,binding_host_id='compute')
        obj=NS(root=self.root,cfg=dict(run='run',inventory='inventory',capture_directory='/var/lib/owned',interval=.2),
               cloud=cloud,pair=lambda stage:pair)
        capture=c.Capture(obj)
        def transport(action, cp):
            intent=json.loads(capture.path.read_text())
            if action=='resolve':
                self.assertEqual(intent['status'],'START_INTENT')
                return dict(status='RESOLVED',tap='tapABC',integration_bridge='br-int')
            if intent['status']=='START_INTENT':
                self.assertEqual(action,'start'); self.assertEqual(cp['port'],'port'); self.assertTrue(cp['allow_create'])
                self.assertEqual(intent['tap'],'tapABC')
            return dict(status='RUNNING',tap='tapABC',path=cp['path'],started_at=1,config=cp,
                        supervisor={'pid':1},tcpdump={'pid':2})
        capture.transport=Mock(side_effect=transport)
        with patch.object(c.subprocess,'check_output',return_value=json.dumps({'_meta':{'hostvars':{'compute':{}}},'compute':{'hosts':['compute']}})):
            capture.start()
        before=cloud.compute.get_server.call_count
        cloud.compute.get_server.side_effect=AssertionError('Nova outage'); cloud.network.get_port.side_effect=AssertionError('Neutron outage')
        capture.start(); self.assertEqual(cloud.compute.get_server.call_count,before)
        self.assertFalse(capture.transport.call_args.args[1]['allow_create'])

    def test_resume_intent_with_missing_remote_journal_cannot_launch(self):
        cfg=dict(self.cfg,allow_create=False)
        with patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'journal is absent'): c.agent_start(self.root,cfg)
            launch.assert_not_called()

    def test_changed_capture_process_identity_is_rejected_on_resume(self):
        cp=dict(self.cfg,status='RUNNING',remote={'supervisor':{'pid':1},'tcpdump':{'pid':2}})
        obj=NS(root=self.root,cfg={}); capture=c.Capture(obj); c.save(capture.path,cp)
        capture.transport=Mock(return_value={'status':'RUNNING','tap':'tapABC','supervisor':{'pid':10},'tcpdump':{'pid':2}})
        with self.assertRaisesRegex(RuntimeError,'identity changed'): capture.start()

    def test_finish_uses_only_pcap_even_when_nova_diagnostics_are_absent(self):
        write_pcap(self.root/'measure0.pcap')
        rows,_=c.echo_rows(self.root/'measure0.pcap',self.cfg['source_ip'],self.cfg['peer_ip'])
        def anchor(row):
            return dict(index=row['index'],timestamp=row['ts'],reply_timestamp=row['reply_timestamp'],
                        identifier=row['identifier'],icmp_sequence=row['icmp_sequence'])
        c.save(self.root/'validation-window.json',dict(pcap_start=anchor(rows[0]),pcap_end=anchor(rows[-1])))
        remote=dict(status='STOPPED',started_at=9,stopped_at=100,returncode=0,dropped_packets=0,supervisor_gap=False)
        obj=NS(root=self.root,cfg={'interval':.2},cloud=Mock(side_effect=AssertionError('Nova API inaccessible')))
        capture=c.Capture(obj); c.save(capture.path,dict(self.cfg,remote=remote))
        capture.anchor=Mock(); capture.snapshot=Mock(return_value=remote)
        result=capture.finish()
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['packet_loss_percent'],0)
        self.assertEqual(result['pair_a_boot_continuity'],'UNAVAILABLE')
        self.assertEqual(obj.cloud.mock_calls,[]); capture.snapshot.assert_called_once_with(stop=True)

    def test_finish_without_packet_recovery_stops_and_preserves_pcap(self):
        write_pcap(self.root/'measure0.pcap'); data=(self.root/'measure0.pcap').read_bytes()
        c.save(self.root/'validation-window.json',{})
        obj=NS(root=self.root,cfg={'interval':.2}); capture=c.Capture(obj)
        c.save(capture.path,dict(self.cfg,remote={}))
        capture.anchor=Mock(side_effect=TimeoutError('no recovery')); capture.snapshot=Mock()
        result=capture.finish()
        self.assertEqual(result['status'],'UNAVAILABLE'); self.assertIsNone(result['packet_loss_percent'])
        capture.snapshot.assert_called_once_with(stop=True)
        self.assertEqual((self.root/'measure0.pcap').read_bytes(),data)


    def test_end_requires_five_fresh_requests_after_final_validation_fence(self):
        c.save(self.root/'validation-window.json',{'pcap_start':{'index':5}})
        obj=NS(root=self.root,cfg={'timeout':10}); capture=c.Capture(obj)
        def snapshot(index,endpoint):
            return dict(last_request_index=index,observed_endpoint={'index':endpoint,'timestamp':endpoint*.2},parse_problems=[])
        capture.snapshot=Mock(side_effect=[snapshot(12,10),snapshot(15,13),snapshot(19,17)])
        with patch.object(c.time,'sleep'):
            end=capture.anchor(end=True)
        self.assertEqual(end['index'],17); self.assertEqual(capture.snapshot.call_count,3)
        self.assertEqual(v.read_evidence(self.root,'validation-window.json')['pcap_end_fence'],12)

    def test_orphan_pcap_is_never_overwritten(self):
        (self.root/'measure0.pcap').write_bytes(b'owned evidence')
        with patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'Orphaned'): c.agent_start(self.root,self.cfg)
            launch.assert_not_called()
        self.assertEqual((self.root/'measure0.pcap').read_bytes(),b'owned evidence')


    def test_compute_inventory_with_empty_hostvars_is_supported(self):
        inventory={'_meta':{'hostvars':{}},'compute':{'hosts':['compute1']}}
        self.assertEqual(c.inventory_compute_host(inventory,'compute1'),'compute1')

    def test_compute_inventory_alias_and_ambiguous_alias_safety(self):
        inventory={'_meta':{'hostvars':{'alias':{'ansible_hostname':'compute1'}}},'compute':{'children':['compute-region']},
                   'compute-region':{'hosts':['alias']}}
        self.assertEqual(c.inventory_compute_host(inventory,'compute1'),'alias')
        inventory['compute-region']['hosts'].append('duplicate')
        inventory['_meta']['hostvars']['duplicate']={'ansible_hostname':'compute1'}
        with self.assertRaisesRegex(RuntimeError,'exactly one'): c.inventory_compute_host(inventory,'compute1')

    def test_non_compute_inventory_host_cannot_be_capture_target(self):
        inventory={'_meta':{'hostvars':{'controller':{}}},'compute':{'hosts':['compute1']},'control':{'hosts':['controller']}}
        with self.assertRaises(RuntimeError): c.inventory_compute_host(inventory,'controller')

    def test_non_owned_files_cannot_become_capture_cleanup_directory(self):
        (self.root/'unrelated').write_text('preserve')
        with patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'non-owned'): c.agent_start(self.root,self.cfg)
            launch.assert_not_called()
        self.assertEqual((self.root/'unrelated').read_text(),'preserve')


if __name__=='__main__': unittest.main()
