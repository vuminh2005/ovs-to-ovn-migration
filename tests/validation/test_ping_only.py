"""Ping-only orchestration, fresh evidence, reports and sender-log export."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import yaml

from tests.support import ROOT
from tests.validation.test_validation import packet, ready_evidence
from tests.diagnostics.test_ns_expanded_probes import free_ports
from tests.recovery import test_live_recovery as recovery_tests
import workload_validation as w
import extended_validation as ext
import ns_probe
import ns_icmp
import ns_log_transfer as transfer
import ns_diagnostics as diagnostics
import rollback_validate


def test_default_profile_is_ping_only_and_tcp_is_opt_in():
    defaults=yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
    ns=yaml.safe_load((ROOT/'group_vars/ns-lab.yml').read_text())
    assert defaults['validation_ping_only'] is True
    assert defaults['validation_pair_d_enabled'] is False
    assert ns['ns_tcp_enabled'] is False and ns['ns_icmp_enabled'] is True
    assert ns['validation_ew_matrix_enabled'] is True


@pytest.mark.parametrize('action',['pre','precutover-ready','post'])
def test_ping_only_orchestration_never_creates_or_waits_for_non_ping_pairs(tmp_path,monkeypatch,action):
    v=Mock();v.root=tmp_path
    v.cfg=dict(ping_only=True,pair_d_enabled=False,placement_enabled=True,run='run')
    v.state=dict(schema_version=2)
    v.collect.return_value={}
    monkeypatch.setattr(w,'Validation',lambda root:v)
    monkeypatch.setattr(sys,'argv',['workload_validation.py',action,str(tmp_path)])
    assert w.main()==0
    assert v.create.call_args_list==([(('measure',),{})] if action=='pre' else [])
    v.wait.assert_not_called();v.prepare_dhcp.assert_not_called();v.verify_precutover.assert_not_called()
    v.post_cutover_dhcp.assert_not_called()
    if action!='pre':v.audit_placement.assert_called_once_with(['measure'])
    assert w.read_evidence(tmp_path,'pair-d-tcp.json')['status']=='DISABLED'


@pytest.mark.parametrize('stage',['pre','post','tcp'])
def test_direct_vm_creation_cannot_bypass_ping_profile(stage):
    v=w.Validation.__new__(w.Validation);v.cfg=dict(ping_only=True,pair_d_enabled=True);v.cloud=Mock()
    with pytest.raises(RuntimeError,match='Ping-only validation prohibits'):v.create(stage)
    assert v.cloud.mock_calls==[]


def ping_evidence(root):
    (root/'metrics').mkdir()
    w.save(root/'runtime.json',dict(phase_marker_schema_version=2))
    ready_evidence(root)
    for name in ('initial-workload-checks.json','pre-workload-checks.json','post-workload-checks.json',
                 'existing-migration-baseline.json','existing-mtu-automatic.json','existing-mtu-remediation.json',
                 'dhcp-initial-preparation.json','dhcp-precutover-preparation.json','post-ovn-bindings.json'):
        (root/name).unlink()
    w.save(root/'validation-config.json',dict(ping_only=True,capture_enabled=True,placement_enabled=True,
        ew_matrix_enabled=True,pair_d_enabled=False,ns=dict(enabled=True,tcp_enabled=False,icmp_enabled=True,diagnostics_enabled=True)))
    w.save(root/'validation-resources.json',dict(schema_version=2,pre=dict(measure={})))
    for name in ('workload-placement.json','ew-matrix-results.json','ns-results.json','ns-diagnostics.json','extended-cleanup.json'):
        w.save(root/name,dict(status='PASS'))


def test_report_succeeds_without_b_c_or_tcp_and_marks_them_disabled(tmp_path,monkeypatch):
    ping_evidence(tmp_path)
    assert w.validation_ready(tmp_path)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/reporting/migration_report.py'),str(tmp_path),'run','inventory'],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    report=w.read_evidence(tmp_path,'migration-report.json')
    assert report['result']=='SUCCESS'
    assert report['existing_workload_migration']['status']==report['fresh_ovn_provisioning']['status']=='DISABLED'
    assert report['pair_d_tcp']['status']=='DISABLED'
    assert not report['north_south']['tcp_enabled']
    assert not any(name.startswith('ew_tcp/') for name in report['traffic_timeline'])
    assert '(TCP)' not in (tmp_path/'migration-report.txt').read_text()
    v=w.Validation.__new__(w.Validation);v.root=tmp_path
    v.cfg=w.read_evidence(tmp_path,'validation-config.json')
    v.state=w.read_evidence(tmp_path,'validation-resources.json');v.cleanup=Mock()
    extra_cleanup=Mock();monkeypatch.setattr(ext,'Extended',lambda _:NS(cleanup=extra_cleanup))
    assert v.finalize() is True
    v.cleanup.assert_called_once_with('pre');extra_cleanup.assert_called_once_with()


@pytest.mark.parametrize('artifact',['measure-readiness.json','measure-post-checks.json','tenant-dataplane-probe.json',
                                   'ew-matrix-results.json','ns-results.json','ns-diagnostics.json',
                                   'existing-network-semantics.json','resource-consistency.json'])
def test_ping_only_does_not_weaken_actual_ping_capture_or_preservation_gates(tmp_path,artifact):
    ping_evidence(tmp_path)
    assert w.validation_ready(tmp_path)
    w.save(tmp_path/artifact,dict(status='UNAVAILABLE'))
    assert not w.validation_ready(tmp_path)


def test_capacity_is_six_ping_vms_three_per_compute(tmp_path):
    cloud=Mock();cloud.session.get_project_id.return_value='project'
    cloud.compute.get_quota_set.return_value=NS(instances=6,cores=6,ram=6144)
    cloud.compute.find_flavor.return_value=NS(id='flavor',vcpus=1,ram=1024,disk=8)
    cloud.compute.servers.return_value=[]
    cloud.compute.get_limits.return_value=NS(absolute=NS(total_cores_used=0,total_ram_used=0))
    cloud.compute.hypervisors.return_value=[NS(name=name,memory_size=3072,memory_used=0,local_disk_size=24,local_disk_used=0)
                                            for name in ('compute1','compute2')]
    cfg=dict(run='run',flavor='flavor',ping_only=True,ew_matrix_enabled=True,pair_d_enabled=False,ns=dict(enabled=True),
             placement={str(i):dict(hypervisor=name,nova_host=name,inventory_host=name) for i,name in enumerate(('compute1','compute2'))})
    x=ext.Extended(NS(root=tmp_path,cfg=cfg,cloud=cloud));x.capacity()
    evidence=w.read_evidence(tmp_path,'workload-capacity.json')
    assert evidence['max_instances']==6 and evidence['max_per_compute']==3
    cloud.compute.update_quota_set.assert_not_called()


def test_ns_ping_config_and_results_have_no_application_tcp_streams(tmp_path):
    cfg=dict(run='run',lifetime=30,tcp_interval=.5,tcp_timeout=1,interval=.2,
             ns=dict(enabled=True,tcp_enabled=False,icmp_enabled=True,icmp_sender_export_enabled=True,
                     port=18180,new_port=18181,control_port=18182))
    x=ext.Extended(NS(root=tmp_path,cfg=cfg,cloud=Mock()));x.state['ns']=dict(control_token='owned')
    guest=x.ns_config(source='10.0.0.2',target='10.1.0.1',label='snat')
    assert guest['tcp_enabled'] is False and guest['icmp_enabled'] is True
    assert guest['icmp_export_enabled'] is True and guest['control_token']=='owned'
    assert 'new_port' not in guest and 'control_port' not in guest
    rows={name:dict(run='run',boot='one',probe_id=name,mono=10,streams={},icmp=dict(alive=True,last_reply_mono=9.9))
          for name in ('snat','fip')}
    baseline=dict(clients=json.loads(json.dumps(rows)))
    x.ns_results(rows,{},baseline)
    result=w.read_evidence(tmp_path,'ns-results.json')
    assert result['status']=='PASS'
    assert all(r['tcp_status']=='DISABLED' and not r['streams'] for r in result['directions'].values())
    rows['snat']['boot']='replacement';x.ns_results(rows,{},baseline)
    assert w.read_evidence(tmp_path,'ns-results.json')['status']=='FAIL'
    x.ns_arm();assert not (tmp_path/'ns-arm.json').exists()


def test_capture_filter_excludes_tcp_when_only_ping_is_enabled(tmp_path):
    cfg=dict(kind='endpoint',run='run',endpoint_ip='10.1.0.187',snat_ip='10.1.0.4',fip_ip='10.1.0.5',
             guest_ip='10.233.0.11',tcp_port=18180,tcp_enabled=False,icmp_enabled=True,lifetime=30,
             directory=str(tmp_path),interface='eth1')
    bpf=diagnostics.capture_filter(cfg)
    assert 'icmp' in bpf and 'arp' in bpf and 'tcp' not in bpf


def test_recovery_barrier_requires_only_original_icmp_when_ns_tcp_is_disabled():
    reader=recovery_tests.RecoveryTests().reader();reader.cfg['ns']['tcp_enabled']=False
    for row in reader.rows.values():row.update(streams={},armed=False)
    points=reader.fast_points(None)
    assert set(points)=={'ew-icmp/0','ew-icmp/1','snat/icmp','fip/icmp'}
    assert all(value['ready'] for value in points.values())
    reader.rows['snat']['icmp']['alive']=False
    assert not reader.fast_points(None)['snat/icmp']['ready']
    reader.rows['fip']['icmp']['pid']=987
    with pytest.raises(ValueError,match='ping identity changed'):reader.fast_points(None)


def test_ping_sender_file_export_works_without_tcp_application_streams(tmp_path,monkeypatch):
    (tmp_path/'ns').mkdir()
    source=tmp_path/'original.jsonl'
    source.write_text(''.join(json.dumps(dict(run='run',label='snat',boot='one',log_id='log',ping_pid=42,
        event_index=i,observed_ts=100+i*.2,observed_mono=200+i*.2,kind='start' if i==1 else 'reply'))+'\n' for i in range(1,41)))
    original=source.read_bytes()
    port,new,control=free_ports(3)
    cfg=dict(run='run',bind='127.0.0.1',source='127.0.0.1',target='127.0.0.1',port=port,new_port=new,control_port=control,
             tcp_enabled=False,icmp_enabled=True,icmp_export_enabled=True,icmp_output=str(source),
             icmp_receive_path=str(tmp_path/'ns/snat-icmp-sender.full.jsonl'),control_token='private',server='endpoint',
             console=False,label='snat',timeout=.5,lifetime=20,output=str(tmp_path/'summary.jsonl'))
    monkeypatch.setattr(ns_icmp,'Ping',lambda *a,**kw:NS(boot='one',snapshot=lambda:dict(log_id='log',pid=42),run=lambda:None,close=lambda:None))
    server=ns_probe.Server(cfg);client=ns_probe.Client(cfg)
    worker=threading.Thread(target=client.run,daemon=True);worker.start()
    try:
        w.save(tmp_path/'ns-baseline.json',dict(clients=dict(snat=dict(boot='one',probe_id=client.probe_id,icmp=dict(log_id='log',pid=42)))))
        w.save(tmp_path/'ns-diagnostics.json',dict(captures={name:dict(remote=dict(status='STOPPED')) for name in ('endpoint','guest-snat','guest-fip')}))
        x=NS(root=tmp_path,cfg=dict(run='run',ns=dict(endpoint_ip='127.0.0.1',port=port)),state=dict(ns=dict(control_token='private')))
        receipt=transfer.collect(x,timeout=6)
        assert receipt['event_count']==40 and Path(cfg['icmp_receive_path']).read_bytes()==original
        assert source.read_bytes()==original and client.stats=={} and not client.armed
        assert set(server.listeners)=={port} and server.opened is None
        with socket.create_connection(('127.0.0.1',port),1) as sock:
            transfer.send(sock,dict(run='run',probe='TCP-echo-must-not-run'))
            assert sock.recv(1)==b''
        assert server.opened is None
    finally:
        client.stop.set();worker.join(3)
        for _,listener in server.listeners.values():listener.shutdown();listener.server_close()


def test_rollback_ping_only_accepts_fresh_packets_without_guest_health_or_pair_b(tmp_path,monkeypatch):
    w.save(tmp_path/'validation-config.json',dict(run='run',ping_only=True,interval=.2))
    w.save(tmp_path/'validation-resources.json',dict(schema_version=2))
    counter={};v=Mock()
    v.pair.side_effect=lambda role:({'0':dict(server='vm0',record_vm='measure0'),'1':dict(server='vm1',record_vm='measure1')}
                                  if role=='measure' else (_ for _ in ()).throw(AssertionError('Pair B must not be read')))
    def console(server,deadline):
        seq=counter.get(server,0);counter[server]=seq+10
        return '\n'.join(w.PREFIX+json.dumps(dict(packet(i),run='run',vm='measure'+server[-1])) for i in range(seq+1,seq+11))
    v.console_output.side_effect=console
    monkeypatch.setattr(w,'Validation',lambda root:v)
    monkeypatch.setattr(rollback_validate.time,'sleep',lambda _:None)
    result=rollback_validate.wait(tmp_path,2)
    assert result['status']=='PASS'
    assert result['checks']=={'measure/0':dict(connectivity='PASS'),'measure/1':dict(connectivity='PASS')}
    v.cloud.network.get_network.assert_not_called()
