#!/usr/bin/env python3
"""Fresh external path evidence and exact legacy retirement; never remove uplinks."""
import configparser
import json
import pathlib
import subprocess
import sys
import time
from ovn_workload_evidence import value
from ns_scope import require, one


def command(argv): return subprocess.check_output(argv,text=True,stderr=subprocess.PIPE,timeout=15)


def table(name, columns):
    raw=json.loads(command(['docker','exec','openvswitch_vswitchd','ovs-vsctl','--format=json','--columns='+columns,'list',name]))
    require(raw['headings']==columns.split(','), 'malformed OVS headings')
    return [dict(zip(raw['headings'],map(value,row))) for row in raw['data']]


def read():
    interfaces=table('Interface','_uuid,name,type,external_ids,options')
    ports=table('Port','_uuid,name,interfaces,external_ids')
    bridges=table('Bridge','_uuid,name,ports')
    def refs(v): return v if isinstance(v,list) else [v]
    for p in ports:
        p['interfaces']=[i for i in interfaces if i['_uuid'] in refs(p['interfaces'])]
        p['bridge']=one([b['name'] for b in bridges if p['_uuid'] in refs(b['ports'])], 'port belongs to ambiguous bridge')
    namespaces={}; namespace_ids={}
    for line in command(['ip','netns','list']).splitlines():
        ns=line.split()[0]
        if ns.startswith(('qrouter-','qdhcp-')):
            info=(pathlib.Path('/var/run/netns')/ns).stat(); namespace_ids[ns]=dict(inode=info.st_ino,device=info.st_dev)
            namespaces[ns]=json.loads(command(['ip','netns','exec',ns,'ip','-j','link']))
    ext=one(table('Open_vSwitch','external_ids'),'expected one OVS database')['external_ids']
    return dict(namespace_ids=namespace_ids,ports=ports,bridges=[b['name'] for b in bridges],external_ids=ext,namespaces=namespaces,
                links=json.loads(command(['ip','-j','address'])),collected_at=time.time())


def verify_path(cfg, host, evidence, target=False):
    if host not in cfg['gateways']:
        require(not evidence['external_ids'].get('ovn-bridge-mappings'), 'provider compute mapping unsupported')
        return
    g=cfg['gateways'][host]; bridge=g['bridge']; uplink=g['uplink']
    require(evidence['external_ids'].get('hostname')==g['chassis_hostname'] and evidence['external_ids'].get('system-id'), 'gateway host/chassis identity evidence mismatch')
    require(bridge in evidence['bridges'], 'external bridge missing on '+host)
    p=one([p for p in evidence['ports'] if p['name']==uplink], 'exact physical/VLAN uplink missing')
    require(p['bridge']==bridge and len(p['interfaces'])==1 and p['interfaces'][0]['type'] in ('','system'), 'uplink is not a physical/system port on intended bridge')
    link=one([l for l in evidence['links'] if l['ifname']==uplink], 'uplink kernel interface missing')
    require(link.get('address','').lower()==g['uplink_mac'].lower(), 'uplink MAC identity mismatch')
    require(link['mtu']==g['uplink_mtu'] and link['mtu']>=cfg['mtu'] and 'UP' in link['flags'], 'uplink MTU/state mismatch')
    require(not link.get('addr_info'), 'uplink carries host/management addressing; unsupported removal risk')
    if target:
        require(evidence['external_ids'].get('ovn-bridge-mappings')==cfg['physnet']+':'+bridge,
                'OVN bridge mappings mismatch on '+host)
        require('enable-chassis-as-gw' in evidence['external_ids'].get('ovn-cms-options','').split(','), 'gateway CMS role missing')
    else:
        parser=configparser.ConfigParser(interpolation=None)
        parser.read('/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini')
        require(parser.get('ovs','bridge_mappings',fallback='').replace(' ','')==cfg['physnet']+':'+bridge, 'source OVS bridge mapping differs from reviewed external path')


def cleanup_plan(cfg, snapshot, evidence):
    owned={p['id']:p for p in snapshot['legacy_ports']}; remove=[]; namespaces=[]
    for ns,links in evidence['namespaces'].items():
        if ns not in snapshot['legacy_namespaces']: continue
        namespaces.append(dict(name=ns,links=[dict(name=l['ifname'].split('@')[0],mac=l.get('address')) for l in links],**evidence['namespace_ids'][ns]))
        for link in links:
            if link['ifname']=='lo': continue
            name=link['ifname'].split('@')[0]
            matches=[p for p in evidence['ports'] if p['name']==name]
            if not matches:  # Linux-only interfaces are removed with this exact namespace, not discovered elsewhere.
                continue
            p=one(matches,'ambiguous namespace OVS port'); interface=one(p['interfaces'],'namespace OVS port must have one interface')
            source=owned.get(interface['external_ids'].get('iface-id'))
            require(source and (source['device']==ns[len('qrouter-'):] if ns.startswith('qrouter-') else source['network']==ns[len('qdhcp-'):] and source['owner']=='network:dhcp') and
                    source['mac'].lower()==link['address'].lower() and
                    p['bridge'] in ['br-int']+[g['bridge'] for g in cfg['gateways'].values()] and
                    not any(k.startswith('ovn') for k in interface['external_ids']), 'legacy namespace port lacks exact API/MAC ownership evidence')
            remove.append(p)
    for g in cfg['gateways'].values():
        bridge=g['bridge']; a='int-'+bridge; b='phy-'+bridge
        pair=[p for p in evidence['ports'] if p['name'] in (a,b)]
        if not pair: continue
        require(len(pair)==2, 'ambiguous legacy OVS-agent external patch pair')
        for p in pair:
            i=one(p['interfaces'],'ambiguous patch interface')
            require(i['type']=='patch' and i['options'].get('peer')==({a:b,b:a}[p['name']]) and
                    p['bridge']==({a:'br-int',b:bridge}[p['name']]) and not p['external_ids'] and
                    not any(k.startswith('ovn') for k in i['external_ids']), 'patch is not a proven legacy OVS-agent path')
        remove.extend(pair)
    require(not any(p['name'] in [g['uplink'] for g in cfg['gateways'].values()] for p in remove), 'never remove uplinks')
    return dict(ports=remove,namespaces=namespaces)


def retire(plan, evidence, namespaces=False):
    # Caller persists intent/evidence before invoking this operation. Exact current
    # OVS UUID/iface-id/type/peer/bridge must still equal the source observation.
    for container in ('neutron_l3_agent','neutron_dhcp_agent','neutron_openvswitch_agent'):
        result=subprocess.run(['docker','inspect',container],capture_output=True,text=True,check=False)
        if result.returncode==0: require(not json.loads(result.stdout)[0]['State']['Running'], 'legacy agent still running')
        else: require('No such' in result.stderr, 'cannot verify legacy-agent shutdown')
    for p in plan['ports']:
        matches=[r for r in evidence['ports'] if r['name']==p['name']]
        if matches:
            require(one(matches,'duplicate current port')==p, 'legacy port changed/reused; retirement refused')
    if namespaces:
        for record in plan['namespaces']:
            if record['name'] in evidence['namespaces']:
                require(evidence['namespace_ids'][record['name']]=={k:record[k] for k in ('inode','device')}, 'namespace identity changed/reused')
                require(all(dict(name=l['ifname'].split('@')[0],mac=l.get('address')) in record['links'] for l in evidence['namespaces'][record['name']]), 'new/changed interface inside legacy namespace; preserve it')
    for p in plan['ports']:
        if any(r['name']==p['name'] for r in evidence['ports']):
            command(['docker','exec','openvswitch_vswitchd','ovs-vsctl','--if-exists','del-port',p['bridge'],p['name']])
    if namespaces:
        for record in plan['namespaces']:
            ns=record['name']
            if ns in evidence['namespaces']:
                require(evidence['namespace_ids'][ns]=={k:record[k] for k in ('inode','device')}, 'namespace identity changed/reused')
                command(['ip','netns','delete',ns])


def main():
    payload=json.load(sys.stdin); evidence=read()
    if payload['action']=='read':
        verify_path(payload['config'],payload['host'],evidence,payload.get('target',False))
        if not payload.get('target'):
            evidence['cleanup']=cleanup_plan(payload['config'],payload['snapshot'],evidence)
    else: retire(payload['plan'],evidence,payload['action']=='cleanup')
    print(json.dumps(evidence))


if __name__=='__main__': main()
