#!/usr/bin/env python3
"""Read exact LSP/SB binding and referenced IPv4 DHCP_Options; no mutations."""
import argparse
import json
import subprocess


def value(item):
    if isinstance(item,list) and len(item)==2 and item[0]=='map':
        return {k:value(v) for k,v in item[1]}
    if isinstance(item,list) and len(item)==2 and item[0]=='set':
        return [value(v) for v in item[1]]
    if isinstance(item,list) and len(item)==2 and item[0] in ('uuid','named-uuid'):
        return item[1]
    return item


def query(tool,db,table,columns,condition):
    command=['docker','exec','ovn_northd',tool,'--timeout=10','--db='+db,'--format=json','--columns='+columns,'find',table,condition]
    raw=json.loads(subprocess.check_output(command,text=True))
    return [dict(zip(raw['headings'],map(value,row))) for row in raw['data']]


def evidence(port,nb,sb):
    binding=query('ovn-sbctl',sb,'Port_Binding','logical_port,chassis,up','logical_port='+port)
    lsp=query('ovn-nbctl',nb,'Logical_Switch_Port','name,dhcpv4_options','name='+port)
    options=[]
    if len(lsp)==1:
        refs=lsp[0]['dhcpv4_options']
        refs=refs if isinstance(refs,list) else [refs]
        if len(refs)==1:
            options=query('ovn-nbctl',nb,'DHCP_Options','_uuid,cidr,external_ids,options','_uuid='+refs[0])
    return dict(port=port,bindings=binding,lsps=lsp,dhcp_options=options)


def main():
    p=argparse.ArgumentParser(); p.add_argument('port'); p.add_argument('nb'); p.add_argument('sb'); a=p.parse_args()
    print(json.dumps(evidence(a.port,a.nb,a.sb)))

if __name__=='__main__': main()
