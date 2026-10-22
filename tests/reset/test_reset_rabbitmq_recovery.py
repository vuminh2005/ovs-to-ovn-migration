"""Exercise the production recovery with real subprocesses and modeled service CLIs."""
import copy
import functools
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests.support import ROOT
sys.path.insert(0, str(ROOT/'scripts'))
import reset_rabbitmq_health as health
import reset_rabbitmq_recovery as recovery


def failure():
    row = dict(observed_at=health.utc(), issues=[
        'scheduler check failed', 'scheduler must be a running quorum queue with consumers',
        'New quorum/Raft errors during readiness observation'],
        checks={n: dict(rc=65 if n == 'scheduler' else 0) for n in
                ('running', 'alarms', 'queues', 'conductor', 'scheduler')},
        queues={'conductor': dict(type='quorum', state='running', consumers=5)},
        members={'conductor': [dict(node='rabbit@controller', state='leader', membership='voter')]},
        log=dict(continuity='CONTINUOUS', errors=[
            'reason: {already_started,<0.17754.0>}',
            "ra: failed to form a new cluster '%2F_scheduler'.",
            "Cannot declare quorum queue 'scheduler' in vhost '/': cluster_not_formed"]))
    return dict(status='FAIL', ended_at=health.utc(), samples=[row])


SHIM = r'''
import json,os,sys,subprocess
from pathlib import Path
root=Path(os.environ['RECOVERY_TEST_ROOT'])
path=root/'model.json'; s=json.loads(path.read_text()); a=sys.argv[1:]
engine=Path(sys.argv[0]).name
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([engine,*a])+'\n')
def done(value='',rc=0):
    temp=root/('model-'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(s));temp.replace(path);print(value);raise SystemExit(rc)
def state(name):
    if name=='rabbitmq' and s.get('boot_polls',0):
        s['boot_polls']-=1
        return {'Running':False,'Restarting':False,'Status':'exited'}
    running=s['rabbit_running' if name=='rabbitmq' else 'scheduler_running']
    return {'Running':running,'Restarting':False,'Status':'running' if running else 'exited'}
if engine=='systemctl':
    op=a[0];unit=next(x for x in a if x.endswith('.service'))
    name='nova_scheduler' if 'nova_scheduler' in unit else 'rabbitmq'
    if op=='show':
        if '--value' in a:done('inactive' if not s['scheduler_running'] else 'active')
        target='unrelated' if s.get('foreign_unit') else name
        done('Id='+unit+'\nLoadState=loaded\nActiveState=active\nExecStart={ path=/usr/bin/docker ; argv[]=/usr/bin/docker start -a '+target+' ; }\nExecStop={ path=/usr/bin/docker ; argv[]=/usr/bin/docker stop '+target+' -t 60 ; }')
    if op=='stop':s['scheduler_running']=False;done()
    if op=='restart':s['boot_polls']=2;s['rabbit_running']=not s.get('boot_failure',False);s['restarts']+=1;done()
    if op=='start':
        if s.get('boot_polls') or not s['rabbit_running']:done('RabbitMQ not ready',92)
        s['scheduler_running']=True;s['recovery_started']=True;done()
if engine=='docker':
    if a[0]=='inspect':
        name=a[-1]
        if a[2]=='{{json .State}}':done(json.dumps(state(name)))
        mounts=[{'Type':'bind' if s.get('custom_mount') else 'volume','Name':'rabbitmq','Destination':'/var/lib/rabbitmq'}]
        done(json.dumps({'image':'fixture','image_id':'sha256:fixture','state':state(name),'mounts':mounts}))
    if a[0]=='exec':
        if any('ConfigParser' in x for x in a):
            code=a[-1].replace('/etc/nova/nova.conf',str(root/'nova.conf'))
            done(subprocess.check_output([sys.executable,'-c',code],text=True))
        if 'check_running' in a:done(rc=0 if s['rabbit_running'] else 1)
        if 'check_local_alarms' in a:done()
        if 'list_queues' in a:
            if not s.get('recovery_started') and not s.get('healed'):
                with (root/'rabbit.log').open('a') as f:f.write("reason: {already_started,<0.17754.0>}\nCannot declare quorum queue 'scheduler': cluster_not_formed\n")
                done('conductor\tquorum\trunning\t5')
            if s.get('post_failure'):
                with (root/'rabbit.log').open('a') as f:f.write("new already_started\nqueue 'scheduler': cluster_not_formed\n")
            done('conductor\tquorum\trunning\t5\nscheduler\tquorum\trunning\t'+('0' if s.get('post_failure') else '1'))
        if 'quorum_status' in a:
            if a[-1]=='scheduler' and not s.get('recovery_started') and not s.get('healed'):done('Object not found',65)
            done('| rabbit@controller | leader | voter |')
done('Unsupported CLI call: '+str(a),91)
'''


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.log=self.root/'rabbit.log'
        self.log.write_text('captured scheduler startup failure\n')
        self.nova=self.root/'nova.log';self.nova.write_text('Starting 5 workers\n')
        self.inventory=self.root/'inventory';self.inventory.write_text('[control]\ncontroller\n')
        (self.root/'multinode.before').write_bytes(self.inventory.read_bytes())
        health.save(self.root/'reset-rabbitmq-policy.json',dict(scope='four-node-lab-reset',confirmed=True,
            inventory=str(self.inventory),control_hosts=['controller'],scheduler_workers=1))
        health.save(self.root/'deploy-result.json',dict(rc=0,inventory=str(self.inventory)))
        health.save(self.root/'rabbitmq-storage-after-destroy-controller.json',
                    dict(host='controller',rc=0,remaining_rabbitmq_volumes=[]))
        health.save(self.root/'rabbitmq-health.json',failure())
        self.model=dict(workers=1,rabbit_running=True,scheduler_running=True,restarts=0)
        self.save_model()
        self.bin=self.root/'bin';self.bin.mkdir()
        for name in ('docker','systemctl'):
            p=self.bin/name;p.write_text('#!'+sys.executable+'\n'+SHIM);p.chmod(0o700)
        env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],RECOVERY_TEST_ROOT=str(self.root))
        self.environ=patch.dict(os.environ,env);self.environ.start();self.addCleanup(self.environ.stop)
        self.nova_patch=patch.object(recovery,'NOVA_LOG',self.nova);self.nova_patch.start();self.addCleanup(self.nova_patch.stop)

    def save_model(self):
        health.save(self.root/'model.json',self.model)
        (self.root/'nova.conf').write_text('[scheduler]\nworkers = '+str(self.model['workers'])+
            '\n[conductor]\nworkers = '+str(self.model.get('conductor_workers',1))+'\n')

    def calls(self):
        p=self.root/'calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def run_recovery(self):
        original=health.wait
        def wait(*args,**kwargs):
            return original(*args,**dict(kwargs,timeout=2,stable_seconds=.02,interval=.01))
        with patch.object(health,'wait',side_effect=wait):
            return recovery.Recovery(self.root,1,self.log).recover()

    def test_real_subprocess_recovery_waits_through_not_running_and_preserves_original_failure(self):
        before=(self.root/'rabbitmq-health.json').read_bytes()
        value=self.run_recovery()
        self.assertEqual(value['status'],'PASS',value)
        self.assertFalse(value['scheduler_may_be_stopped'])
        self.assertEqual((self.root/'rabbitmq-health.json').read_bytes(),before)
        self.assertEqual(json.loads((self.root/'rabbitmq-health-after-recovery.json').read_text())['status'],'PASS')
        calls=self.calls();mutations=[x for x in calls if x[0]=='systemctl' and x[1] in ('stop','restart','start')]
        self.assertEqual(mutations,[['systemctl','stop',recovery.UNITS['nova_scheduler']],
            ['systemctl','restart',recovery.UNITS['rabbitmq']],['systemctl','start',recovery.UNITS['nova_scheduler']]])
        stop=calls.index(mutations[0]);restart=calls.index(mutations[1])
        self.assertTrue(any(x[:2]==['docker','inspect'] and x[-1]=='nova_scheduler' for x in calls[stop+1:restart]))
        for label in ('before','after'):
            d=self.root/('rabbitmq-recovery-'+label)
            self.assertEqual(gzip.decompress((d/'nova.log.gz').read_bytes()),self.nova.read_bytes())

    def test_second_attempt_cannot_restart_or_overwrite_evidence(self):
        self.assertEqual(self.run_recovery()['status'],'PASS')
        saved=(self.root/'rabbitmq-recovery.json').read_bytes();calls=self.calls()
        with self.assertRaisesRegex(RuntimeError,'already evaluated'):self.run_recovery()
        self.assertEqual(self.calls(),calls)
        self.assertEqual((self.root/'rabbitmq-recovery.json').read_bytes(),saved)

    def test_consumed_interrupted_attempt_is_not_retried(self):
        health.save(self.root/'rabbitmq-recovery-attempt.json',dict(status='RECOVERING'))
        with self.assertRaises(RuntimeError):self.run_recovery()
        self.assertFalse(self.calls())

    def test_wrong_workers_block_before_any_service_mutation(self):
        self.model['workers']=5;self.save_model()
        result=self.run_recovery()
        self.assertEqual(result['status'],'FAIL')
        self.assertIn('workers differ',result['error'])
        self.assertFalse(any(c[0]=='systemctl' for c in self.calls()))

    def test_conductor_worker_verification_reads_its_own_container_and_section(self):
        self.model.update(workers=5,conductor_workers=1);self.save_model()
        value=recovery.verify_workers(self.root,1,service='nova_conductor')
        self.assertEqual(value['status'],'PASS')
        self.assertEqual(value['effective_workers'],1)
        self.assertEqual(self.calls()[0][:3],['docker','exec','nova_conductor'])
        self.assertEqual(json.loads((self.root/'nova-conductor-workers.json').read_text())['status'],'PASS')
        self.assertFalse((self.root/'nova-scheduler-workers.json').exists())

    def test_five_conductor_workers_are_rejected_when_one_was_requested(self):
        self.model['conductor_workers']=5;self.save_model()
        with self.assertRaisesRegex(RuntimeError,'conductor workers differ'):
            recovery.verify_workers(self.root,1,service='nova_conductor')
        value=json.loads((self.root/'nova-conductor-workers.json').read_text())
        self.assertEqual(value['status'],'FAIL')
        self.assertEqual(value['effective_workers'],5)
        self.assertFalse(any(c[0]=='systemctl' for c in self.calls()))

    def test_foreign_systemd_unit_is_never_operated(self):
        self.model['foreign_unit']=True;self.save_model()
        result=self.run_recovery()
        self.assertEqual(result['status'],'FAIL')
        self.assertFalse(any(c[0]=='systemctl' and c[1] in ('start','stop','restart') for c in self.calls()))

    def test_custom_data_mount_is_not_automatically_restarted(self):
        self.model['custom_mount']=True;self.save_model()
        result=self.run_recovery()
        self.assertEqual(result['status'],'FAIL')
        self.assertIn('named volume',result['error'])
        self.assertFalse(any(c[0]=='systemctl' and c[1] in ('start','stop','restart') for c in self.calls()))

    def test_failed_deploy_changed_inventory_or_missing_storage_proof_never_restarts(self):
        for bad in ('deploy','inventory','storage','confirmation','scope'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as tmp:
                import shutil
                target=Path(tmp)/'run';shutil.copytree(self.root,target)
                if bad=='deploy':health.save(target/'deploy-result.json',dict(rc=2))
                elif bad=='inventory':(target/'multinode.before').write_text('changed')
                elif bad=='storage':(target/'rabbitmq-storage-after-destroy-controller.json').unlink()
                else:
                    policy=json.loads((target/'reset-rabbitmq-policy.json').read_text())
                    policy['confirmed' if bad=='confirmation' else 'scope']=False
                    health.save(target/'reset-rabbitmq-policy.json',policy)
                result=recovery.Recovery(target,1,self.log).recover()
                self.assertEqual(result['status'],'FAIL')
        self.assertFalse(self.calls())

    def test_persistent_failure_after_restart_is_not_passed(self):
        self.model['post_failure']=True;self.save_model()
        result=self.run_recovery()
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(json.loads((self.root/'model.json').read_text())['restarts'],1)
        self.assertEqual(json.loads((self.root/'rabbitmq-health-after-recovery.json').read_text())['status'],'FAIL')

    def test_live_self_recovery_is_revalidated_without_restart(self):
        self.model['healed']=True;self.save_model()
        result=self.run_recovery()
        self.assertEqual(result['status'],'PASS',result)
        self.assertFalse(result['attempted'])
        self.assertEqual(json.loads((self.root/'model.json').read_text())['restarts'],0)

    def test_rabbit_boot_failure_leaves_scheduler_stopped_and_never_claims_pass(self):
        self.model['boot_failure']=True;self.save_model()
        original=recovery.Recovery.wait_container
        def wait(instance,name,running,seconds=90):
            return original(instance,name,running,seconds=.05 if running else seconds)
        with patch.object(recovery.Recovery,'wait_container',wait):
            result=self.run_recovery()
        self.assertEqual(result['status'],'FAIL')
        self.assertTrue(result['scheduler_may_be_stopped'])
        self.assertFalse(any(c[:2]==['systemctl','start'] for c in self.calls()))

    def test_real_ansible_recovery_task_and_gate_continue_only_after_pass(self):
        scripts=self.root/'scripts/reset';scripts.mkdir(parents=True)
        (scripts/'reset_rabbitmq_recovery.py').write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT/'scripts'))+')\n'
            'from _paths import configure\nconfigure()\n'
            'import reset_rabbitmq_recovery as r\nr.NOVA_LOG=Path('+repr(str(self.nova))+')\nraise SystemExit(r.main())\n')
        deploy=next(p for p in yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
                    if p['name']=='Deploy clean source ML2 OVS cloud')
        names=('Verify deployed Nova scheduler worker configuration',
               'Verify deployed Nova conductor worker configuration',
               'Recover the evidenced scheduler Raft startup failure at most once',
               'Fail when deploy or RabbitMQ validation failed')
        tasks=[copy.deepcopy(t) for t in deploy['tasks'] if t['name'] in names]
        vars=dict(reset_run_dir=str(self.root),reset_nova_scheduler_workers=1,reset_nova_conductor_workers=1,
            reset_rabbitmq_log_path=str(self.log),reset_rabbitmq_health_timeout=15,
            reset_rabbitmq_stable_seconds=1,reset_rabbitmq_recovery_enabled=True,
            reset_deploy={'rc':0},reset_rabbitmq_health={'rc':1},ansible_python_interpreter=sys.executable)
        play=self.root/'test.yml'
        play.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,vars=vars,tasks=tasks)],sort_keys=False))
        result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(play)],
            capture_output=True,text=True,timeout=50)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads((self.root/'rabbitmq-recovery.json').read_text())['status'],'PASS')

    def test_real_ansible_generates_one_worker_and_target_update_retains_it(self):
        reset=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        copies=[t for p in reset for t in p.get('tasks',[]) if 'ansible.builtin.copy' in t]
        task=copy.deepcopy(next(t for t in copies if t['ansible.builtin.copy'].get('dest')=='{{ reset_globals_path }}'))
        target=yaml.safe_load((ROOT/'playbooks/06-target-config.yml').read_text())
        change=copy.deepcopy(next(t for t in target[0]['tasks'] if t['name']=='[06] Write target ML2/OVN settings'))
        path=self.root/'globals.yml'
        vars=dict(reset_globals_path=str(path),reset_config_path=str(self.root/'config'),
            reset_nova_scheduler_workers=1,reset_nova_conductor_workers=1,
            ns_physnet='physnet1',kolla_globals_path=str(path),
            target_neutron_plugin_agent='ovn',neutron_type_drivers_target='flat,vlan,geneve',
            target_tenant_network_type='geneve',ansible_python_interpreter=sys.executable)
        play=self.root/'render.yml'
        play.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,vars=vars,tasks=[task,change])],sort_keys=False))
        result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(play)],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        values=yaml.safe_load(path.read_text())
        self.assertEqual(values['nova_scheduler_workers'],1)
        self.assertEqual(values['nova_cell_conductor_workers'],1)
        self.assertEqual(values['neutron_plugin_agent'],'ovn')


class EligibilityTests(unittest.TestCase):
    def test_current_run_signature_and_deadline_only_final_sample(self):
        data=failure();self.assertTrue(recovery.eligible(data))
        data['samples'].append(dict(checks={n:dict(rc=124,timeout_scope='overall') for n in
            ('running','alarms','queues','conductor','scheduler')},issues=['Overall readiness deadline reached']))
        self.assertEqual(health.last_complete(data),data['samples'][0])
        self.assertTrue(recovery.eligible(data))

    def test_other_failures_or_historical_errors_do_not_authorize_restart(self):
        for mode in ('alarm','transport','other-queue','healthy','consumer-only','parser','gap','pass'):
            data=failure();row=data['samples'][0]
            if mode=='alarm':row['checks']['alarms']['rc']=2
            elif mode=='transport':row['checks']['queues']['rc']=124
            elif mode=='other-queue':row['log']['errors'].append("queue 'conductor': cluster_not_formed")
            elif mode=='healthy':
                row['checks']['scheduler']['rc']=0
                row['queues']['scheduler']=dict(type='quorum',state='running',consumers=1)
            elif mode=='consumer-only':row['log']['errors']=[]
            elif mode=='parser':row['issues'].append('Malformed quorum member row')
            elif mode=='gap':row['log']['continuity']='UNAVAILABLE'
            else:data['status']='PASS'
            with self.subTest(mode=mode):self.assertFalse(recovery.eligible(data))


if __name__=='__main__':unittest.main()
