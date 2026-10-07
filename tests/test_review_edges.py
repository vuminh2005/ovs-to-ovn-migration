"""Concrete safety edges uncovered while reviewing the DHCP/metric diff."""
import importlib.util
import json
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_validation import v, packet, anchor, ready_evidence
import test_server_readiness as readiness

ROOT=pathlib.Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('guest',ROOT/'scripts/guest_probe.py')
g=importlib.util.module_from_spec(spec); spec.loader.exec_module(g)


class ReviewEdges(unittest.TestCase):
    def test_duplicate_metadata_ports_with_same_ip_are_unavailable(self):
        port=SimpleNamespace(device_owner='network:distributed',fixed_ips=[{'subnet_id':'s','ip_address':'10.0.0.3'}])
        self.assertEqual(v.metadata_port_ip([port],'s'),'10.0.0.3')
        self.assertIsNone(v.metadata_port_ip([port,port],'s'))
        self.assertEqual(v.dhcp_convergence({'dhcp':True,'mtu':1442,'metadata_gateway':'10.0.0.3'},1442,v.metadata_port_ip([port,port],'s')),'UNAVAILABLE')

    def test_boundary_pause_cannot_report_zero_outage(self):
        rows=[packet(10)]+[packet(i,epoch=130) for i in range(11,18)]
        result=v.probe_metrics(rows,anchor(10),anchor(17),.2)
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertFalse(result['timing_valid'])
        self.assertIsNone(result['actual_dataplane_outage_seconds'])

    def test_recovery_does_not_drop_earlier_loss(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d); obj.cfg={'interval':.2}
            v.save(obj.root/'validation-window.json',{'start_anchor':anchor(1)})
            rows={key:[packet(i,i not in (3,4,5)) for i in range(1,12)] for key in ('0','1')}
            self.assertTrue(obj.checkpoint_recovery(rows,{'0':anchor(1),'1':anchor(1)}))
            result=v.read_evidence(obj.root,'tenant-dataplane-probe.json')
            self.assertEqual(result['packets_failed'],3)
            self.assertAlmostEqual(result['actual_dataplane_outage_seconds'],.6)

    def test_timer_options_precede_both_server_boots(self):
        with tempfile.TemporaryDirectory() as d:
            obj=readiness.ReadinessTests().obj(pathlib.Path(d)); events=[]
            for key in ('0','1'): del obj.state['pre'][key]['server']
            obj.cloud.network.update_port.side_effect=lambda port,**kw: events.append(('options',port,kw['extra_dhcp_opts']))
            obj.cloud.compute.servers.return_value=[]
            def create(**kwargs):
                events.append(('server',kwargs['name']))
                return SimpleNamespace(id=kwargs['name'],status='BUILD')
            obj.cloud.compute.create_server.side_effect=create
            obj.cloud.compute.get_server.side_effect=lambda uuid:SimpleNamespace(id=uuid,status='ACTIVE')
            obj.create('pre')
            self.assertEqual([e[0] for e in events],['options','options','server','server'])
            self.assertEqual(events[0][2],[{'opt_name':'58','opt_value':'30','ip_version':4},{'opt_name':'59','opt_value':'60','ip_version':4}])

    def test_metadata_route_error_does_not_hide_dhcp_availability(self):
        with tempfile.TemporaryDirectory() as d:
            lease=pathlib.Path(d)/'lease'; lease.write_text('ADDRESS=10.0.0.2')
            addresses=[{'flags':['UP'],'mtu':1442,'addr_info':[{'local':'10.0.0.2'}]}]
            routes=[{'dst':'default','gateway':'10.0.0.1'}]
            opener=Mock(); opener.open.side_effect=OSError('metadata unavailable')
            with patch.object(g,'CONFIG',{'ip':'10.0.0.2','server_id':'uuid'}),patch.object(g.pathlib.Path,'glob',return_value=[lease]),patch.object(g.subprocess,'check_output',side_effect=[json.dumps(addresses),json.dumps(routes),subprocess.CalledProcessError(2,'ip route get')]),patch.object(g.urllib.request,'build_opener',return_value=opener),patch.object(g,'emit') as emit:
                g.health(10)
            record=emit.call_args.args[0]
            self.assertTrue(record['dhcp'])
            self.assertFalse(record['metadata'])
            self.assertIsNone(record['metadata_gateway'])

    def test_final_console_error_keeps_captured_packet_measurement(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); obj=v.Validation.__new__(v.Validation)
            obj.root=root; obj.cfg={'interval':.2}; obj.state={'post':{'cleaned':True}}
            v.save(root/'validation-window.json',{'start_anchor':anchor(1),'end_anchor':anchor(11)})
            v.save(root/'measure0-console-records.json',[packet(i,i not in (3,4,5)) for i in range(1,12)])
            obj.wait=Mock(side_effect=RuntimeError('metadata failed'))
            obj.collect=Mock(side_effect=OSError('Nova API unavailable'))
            with patch.object(v,'Validation',return_value=obj),patch.object(sys,'argv',['workload_validation.py','post',d]):
                self.assertEqual(v.main(),1)
            result=v.read_evidence(root,'tenant-dataplane-probe.json')
            self.assertEqual(result['status'],'PASS')
            self.assertAlmostEqual(result['actual_dataplane_outage_seconds'],.6)

    def test_failed_health_report_keeps_valid_packet_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); (root/'metrics').mkdir(); ready_evidence(root)
            checks=v.read_evidence(root,'pre-workload-checks.json')
            checks['0'].update(metadata='FAIL',dhcp_convergence='FAIL')
            v.save(root/'pre-workload-checks.json',checks)
            v.save(root/'tenant-dataplane-probe.json',{'status':'PASS','measurement_workload':'Pair A','evidence_source':'compute-tap-pcap','packet_loss_percent':3,'actual_dataplane_outage_seconds':.6})
            subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),d,'run','inventory'],check=True,stdout=subprocess.DEVNULL)
            report=v.read_evidence(root,'migration-report.json'); text=(root/'migration-report.txt').read_text()
            self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE')
            self.assertEqual(report['dataplane_probe']['status'],'PASS')
            self.assertIn('Packet loss: 3 %',text)
            self.assertIn('Actual dataplane outage: 0.6 s',text)

    def test_real_frame_decoder_ignores_wrong_guest_and_nak(self):
        def frame(ip='10.0.0.2',message=5):
            body=bytearray(240); body[0]=1 if message==3 else 2; body[12:16]=socket.inet_aton(ip) if message==3 else bytes(4); body[16:20]=socket.inet_aton(ip); body[236:240]=b'\x63\x82\x53\x63'
            body.extend(bytes([53,1,message,58,4])+struct.pack('!I',27)+bytes([59,4])+struct.pack('!I',57)+b'\xff')
            udp=struct.pack('!HHHH',68 if message==3 else 67,67 if message==3 else 68,8+len(body),0)+body
            ipheader=bytearray(20); ipheader[0]=0x45; ipheader[9]=17
            return bytes(12)+b'\x08\x00'+ipheader+udp
        stream=Mock(); stream.recv.side_effect=[frame(ip='10.0.0.9'),frame(message=6),frame(),frame(message=3),frame(),frame(),frame(message=3),frame(),frame(),RuntimeError('stop fixture')]
        context=Mock(); context.__enter__=Mock(return_value=stream); context.__exit__=Mock(return_value=False)
        observed={'ack_count':0,'t1_seconds':None,'t2_seconds':None,'last_ack_monotonic':None}
        with patch.object(g,'CONFIG',{'ip':'10.0.0.2','dhcp_t1':30}),patch.object(g,'DHCP',observed),patch.object(g.socket,'socket',return_value=context),patch.object(g.time,'monotonic',side_effect=[1,29]),patch.object(g,'emit'):
            g.observe_dhcp()
        self.assertEqual(observed['ack_count'],2)
        self.assertEqual(observed['t1_seconds'],27)
        self.assertEqual(observed['t2_seconds'],57)
        self.assertEqual(observed['last_ack_monotonic'],29)
        self.assertEqual(observed['last_renewal_interval_seconds'],28)

    def test_duplicate_ack_cannot_create_renewal_interval(self):
        def frame(message):
            body=bytearray(240); body[0]=1 if message==3 else 2
            body[12:16]=socket.inet_aton('10.0.0.2') if message==3 else bytes(4)
            body[16:20]=socket.inet_aton('10.0.0.2'); body[236:240]=b'\x63\x82\x53\x63'
            body.extend(bytes([53,1,message,58,4])+struct.pack('!I',28)+bytes([59,4])+struct.pack('!I',58)+b'\xff')
            udp=struct.pack('!HHHH',68 if message==3 else 67,67 if message==3 else 68,8+len(body),0)+body
            ipheader=bytearray(20); ipheader[0]=0x45; ipheader[9]=17
            return bytes(12)+b'\x08\x00'+ipheader+udp
        stream=Mock(); stream.recv.side_effect=[frame(3),frame(5),frame(5),frame(5),RuntimeError('stop fixture')]
        context=Mock(); context.__enter__=Mock(return_value=stream); context.__exit__=Mock(return_value=False)
        observed={'ack_count':0,'t1_seconds':None,'t2_seconds':None,'last_ack_monotonic':None,'last_renewal_interval_seconds':None}
        with patch.object(g,'CONFIG',{'ip':'10.0.0.2','dhcp_t1':30}),patch.object(g,'DHCP',observed),patch.object(g.socket,'socket',return_value=context),patch.object(g.time,'monotonic',side_effect=[1,29,57]) as clock,patch.object(g,'emit'):
            g.observe_dhcp()
        self.assertEqual(clock.call_count,1)
        self.assertEqual(observed['ack_count'],1)
        self.assertEqual(observed['last_ack_monotonic'],1)
        self.assertIsNone(observed['last_renewal_interval_seconds'])


if __name__=='__main__': unittest.main()
