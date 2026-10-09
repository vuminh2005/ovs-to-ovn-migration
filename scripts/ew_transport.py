"""Verified existing namespace SSH access, separate from EW measurement."""
import base64
import argparse
import ipaddress
import json
import pathlib
import re
import shlex
import subprocess


def ssh_options(key, known_hosts, timeout):
    if not key or not known_hosts:
        raise RuntimeError('Configure existing EW SSH keys and verified known-hosts files before measurement')
    return ['-i',key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','UserKnownHostsFile='+known_hosts,'-o','ConnectTimeout='+str(max(1,int(min(timeout,10)))),
            '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2']


def verify_profile(profile, vm, expected_boot=None, mtu=None, mac=None):
    if profile.get('server','').lower()!=vm['server'].lower() or not profile.get('boot'):
        raise RuntimeError('SSH guest does not match checkpointed server UUID/boot')
    if expected_boot and profile['boot']!=expected_boot:
        raise RuntimeError('EW guest reboot detected; no automatic remediation is permitted')
    matches=[iface for iface in profile.get('interfaces',[]) if any(
        a.get('local')==vm['ip'] and a.get('family')=='inet' for a in iface.get('addr_info',[]))]
    if len(matches)!=1 or (mac and matches[0].get('address','').lower()!=mac.lower()):
        raise RuntimeError('SSH guest IP/interface/MAC contradicts checkpointed Neutron port')
    if mtu is not None and matches[0].get('mtu')!=mtu:
        raise RuntimeError(f'EW guest MTU must be {mtu}; prepare guest DHCP/MTU separately, never reboot/remediate it here')
    if matches[0].get('operstate') not in ('UP','UNKNOWN'):
        raise RuntimeError('EW guest interface is not usable')
    if not any(r.get('dst')=='default' and r.get('gateway') for r in profile.get('routes',[])):
        raise RuntimeError('EW guest has no usable basic routed configuration')
    return matches[0]


class Transport:
    def __init__(self, config, catalog, inventory=None, cloud=None):
        self.cfg=config; self.catalog=catalog; self.cloud=cloud
        self.access_cache={}
        self.inventory=inventory or json.loads(subprocess.check_output(
            ['ansible-inventory','-i',config['inventory'],'--list'],text=True,timeout=config['transport_timeout']))

    def run(self, argv, data=None, timeout=None):
        timeout=timeout or self.cfg['transport_timeout']
        if getattr(self,'deadline',None) is not None:
            import time
            timeout=min(timeout,self.deadline-time.monotonic())
            if timeout<=0: raise TimeoutError('Bounded EW management operation expired; application state is unknown')
        result=subprocess.run(argv,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                              timeout=timeout)
        if result.returncode:
            error=re.findall(r'\b([A-Za-z]+(?:Error|Expired)):',result.stderr)
            detail=error[-1] if error else ('SSH authentication/host-key/access prerequisite' if any(
                word in result.stderr.lower() for word in ('permission denied','host key','sudo','identity file')) else 'SSH/remote command failure')
            raise RuntimeError(f'Management transport failed (SSH rc={result.returncode}, {detail}); application availability is unknown')
        return result.stdout

    def host_argv(self, host):
        vars=self.inventory.get('_meta',{}).get('hostvars',{}).get(host,{})
        override=self.cfg.get('host_access',{}).get(host,{})
        address=override.get('address',vars.get('ansible_host',host))
        user=override.get('user',vars.get('ansible_user','root'))
        if not re.fullmatch(r'[A-Za-z0-9_.:@-]+',address) or not re.fullmatch(r'[A-Za-z0-9_-]+',user):
            raise RuntimeError('Unsafe inventory SSH address/user')
        return ['ssh',*ssh_options(override.get('key',vars.get('ansible_ssh_private_key_file',vars.get('ansible_private_key_file',self.cfg.get('host_key')))),
                                  self.cfg['host_known_hosts'],self.cfg['transport_timeout']),user+'@'+address]

    def checkpoint_inventory(self):
        """Host identities/key paths only, never inventory secrets/key contents."""
        hosts={}
        for host in self.candidates():
            values=self.inventory.get('_meta',{}).get('hostvars',{}).get(host,{})
            override=self.cfg.get('host_access',{}).get(host,{})
            hosts[host]=dict(ansible_host=override.get('address',values.get('ansible_host',host)),
                ansible_user=override.get('user',values.get('ansible_user','root')),
                ansible_ssh_private_key_file=override.get('key',values.get('ansible_ssh_private_key_file',
                    values.get('ansible_private_key_file',self.cfg.get('host_key')))))
        return {'_meta':{'hostvars':hosts}}

    def host(self, host, argv):
        prefix=[] if self.host_argv(host)[-1].startswith('root@') else ['sudo','-n']
        return self.run(self.host_argv(host)+[shlex.join(prefix+argv)])

    def candidates(self):
        # Explicit inventory names, validated by the Ansible config writer;
        # discovery never assumes every host has an ovnmeta namespace.
        return self.cfg['namespace_hosts']

    def namespace(self, vm, phase):
        if phase=='source':
            name='qrouter-'+self.catalog['router']; metadata=None
        else:
            name='ovnmeta-'+vm['network']
            if self.cloud is None: raise RuntimeError('Post-OVN namespace discovery requires restored Neutron API')
            subnet_ids={f['subnet_id'] for f in vm['fixed_ips']}
            ports=list(self.cloud.network.ports(network_id=vm['network'],device_owner='network:distributed'))
            matches=[(p,f['ip_address']) for p in ports for f in p.fixed_ips if f['subnet_id'] in subnet_ids]
            if len(matches)!=1: raise RuntimeError('Missing/ambiguous distributed metadata port for EW subnet')
            metadata=matches[0]
        if not re.fullmatch(r'(qrouter|ovnmeta)-[0-9a-fA-F-]{36}',name): raise RuntimeError('Unsafe catalog namespace identity')
        for host in self.candidates():
            try:
                namespaces={line.split()[0] for line in self.host(host,['ip','netns','list']).splitlines() if line.strip()}
                if name not in namespaces: continue
                if metadata:
                    addresses=json.loads(self.host(host,['ip','netns','exec',name,'ip','-j','address']))
                    port,ip=metadata
                    if not any(a.get('address','').lower()==port.mac_address.lower() and
                               any(f.get('local')==ip for f in a.get('addr_info',[])) for a in addresses): continue
                self.host(host,['ip','netns','exec',name,'nc','-z','-w','3',str(ipaddress.IPv4Address(vm['ip'])),'22'])
                return dict(host=host,namespace=name,metadata_port=metadata[0].id if metadata else None)
            except (RuntimeError,subprocess.TimeoutExpired,ValueError): continue
        raise RuntimeError('No verified existing '+phase+' namespace can reach this EW guest; local measurement evidence is retained')

    def guest_argv(self, vm, access, timeout=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',self.cfg['guest_user']): raise RuntimeError('Unsafe guest SSH user')
        options=ssh_options(self.cfg['guest_key'],self.cfg['guest_known_hosts'],self.cfg['transport_timeout'])
        if 'direct' in access:
            destination=access['direct']
        else:
            host_args=self.host_argv(access['host'])
            import math
            remote=['ip','netns','exec',access['namespace'],'nc','-w',str(max(1,math.ceil(timeout or self.cfg['transport_timeout']))),vm['ip'],'22']
            if not host_args[-1].startswith('root@'): remote=['sudo','-n',*remote]
            proxy=shlex.join(host_args+[shlex.join(remote)])
            options+=['-o','ProxyCommand='+proxy]
            destination=vm['ip']
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+',destination): raise RuntimeError('Unsafe guest access address')
        return ['ssh',*options,self.cfg['guest_user']+'@'+destination]

    def access(self, vm, phase):
        direct=self.cfg.get('direct_access',{}).get(vm['server'])
        if direct: return dict(direct=direct)
        key=(phase,vm['network'] if phase=='ovn' else self.catalog['router'])
        if key not in self.access_cache: self.access_cache[key]=self.namespace(vm,phase)
        return self.access_cache[key]

    def profile(self, vm, access):
        # Read-only identity handshake BEFORE helper installation or operations.
        agent=pathlib.Path(__file__).parents[1]/'workloads/ew-workload-metrics/agent.py'
        text=agent.read_text(); begin=text.index('def profile():'); end=text.index('\n\ndef unit',begin)
        code='import json,subprocess,time\nfrom pathlib import Path\nfrom datetime import datetime,timezone\n'
        code+='class runner:\n    @staticmethod\n    def utc(): return datetime.now(timezone.utc).isoformat()\n'
        code+='def command(a): return subprocess.check_output(a,text=True,timeout=10)\n'+text[begin:end]+'\nprint(json.dumps(profile()))'
        return json.loads(self.run(self.guest_argv(vm,access)+[shlex.join(['sudo','-n','python3','-c',code])]))

    def install(self, vm, access):
        base=pathlib.Path(__file__).parents[1]/'workloads/ew-workload-metrics'
        files={name:base64.b64encode((base/name).read_bytes()).decode() for name in ('runner.py','agent.py')}
        code="""import json,sys,base64,pathlib
r=pathlib.Path('/opt/ew-load'); r.mkdir(exist_ok=True); r.chmod(0o755)
d=json.load(sys.stdin)
if set(d)!={'agent.py','runner.py'}: raise RuntimeError('Invalid helper filenames')
for name,value in d.items():
    path=r/name; path.write_bytes(base64.b64decode(value,validate=True)); path.chmod(0o644)
"""
        return self.run(self.guest_argv(vm,access)+[shlex.join(['sudo','-n','python3','-c',code])],json.dumps(files))

    def operation(self, vm, access, payload, timeout=None):
        return json.loads(self.run(self.guest_argv(vm,access,timeout)+[
            'sudo -n /usr/bin/python3 /opt/ew-load/agent.py'],json.dumps(payload),timeout))


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('root',type=pathlib.Path)
    p.add_argument('guest'); p.add_argument('phase',choices=('source','ovn')); p.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args(); cfg=json.loads((args.root/'ew-measurement-config.json').read_text())
    catalog=json.loads((args.root/'ew-resources.json').read_text()); state=json.loads((args.root/'ew-lifecycle.json').read_text())
    vm=catalog['servers'][args.guest]; sessions=state['sessions']; session=sessions.get('migration',sessions.get('baseline'))
    if not session or args.guest not in session['guests']: raise RuntimeError('No checkpointed guest boot for verified SSH')
    cloud=None
    if args.phase=='ovn':
        import openstack
        cloud=openstack.connect(api_timeout=3)
    transport=Transport(cfg,catalog,cloud=cloud); access=transport.access(vm,args.phase)
    verify_profile(transport.profile(vm,access),vm,session['guests'][args.guest]['boot'],mac=session['guests'][args.guest]['mac'])
    if not args.command: raise RuntimeError('Specify an explicit SSH command')
    print(transport.run(transport.guest_argv(vm,access)+[shlex.join(args.command)]),end='')


if __name__=='__main__': main()
