#!/usr/bin/env python3
"""Read exact LSP/SB binding and referenced IPv4 DHCP_Options; no mutations."""
import argparse
import json
import subprocess
import uuid


def value(item):
    if isinstance(item,list) and len(item)==2 and item[0]=='map':
        return {k:value(v) for k,v in item[1]}
    if isinstance(item,list) and len(item)==2 and item[0]=='set':
        return [value(v) for v in item[1]]
    if isinstance(item,list) and len(item)==2 and item[0] in ('uuid','named-uuid'):
        return item[1]
    return item


def query(tool,db,table,columns,condition,operation='find'):
    command=['docker','exec','ovn_northd',tool,'--timeout=10','--db='+db,'--format=json','--columns='+columns,operation,table]+([condition] if condition else [])
    try:
        output=subprocess.check_output(command,text=True,stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        # Identify the query without including the database connection string.
        raise RuntimeError(f'{tool} {operation} {table} failed with return code {exc.returncode}; '
                           f'stderr: {exc.stderr or ""}; stdout: {exc.output or ""}') from None
    try:
        raw=json.loads(output)
        headings=raw['headings']; rows=raw['data']
        if (not isinstance(headings,list) or not all(isinstance(h,str) for h in headings)
                or len(set(headings))!=len(headings) or headings!=columns.split(',')
                or not isinstance(rows,list) or not all(isinstance(row,list) and len(row)==len(headings) for row in rows)):
            raise ValueError('invalid headings or row shape')
        return [dict(zip(headings,map(value,row))) for row in rows]
    except (ValueError,TypeError,KeyError) as exc:
        raise RuntimeError(f'{tool} {operation} {table} returned malformed OVN JSON: {exc}') from None


def evidence(port,nb,sb):
    binding=query('ovn-sbctl',sb,'Port_Binding','logical_port,chassis,up','logical_port='+port)
    lsp=query('ovn-nbctl',nb,'Logical_Switch_Port','name,dhcpv4_options','name='+port)
    if len(lsp)!=1 or lsp[0]['name']!=port:
        raise RuntimeError('Expected exactly one Logical_Switch_Port for the exact Neutron port UUID')
    refs=lsp[0]['dhcpv4_options']
    refs=refs if isinstance(refs,list) else [refs]
    if len(refs)!=1 or not isinstance(refs[0],str):
        raise RuntimeError('Expected exactly one dhcpv4_options UUID reference for the validation port')
    dhcp_uuid=refs[0]
    try:
        if str(uuid.UUID(dhcp_uuid))!=dhcp_uuid:
            raise ValueError('noncanonical UUID')
    except ValueError:
        raise RuntimeError('Malformed dhcpv4_options UUID reference for the validation port') from None
    options=query('ovn-nbctl',nb,'DHCP_Options','cidr,external_ids,options',dhcp_uuid,operation='list')
    if len(options)!=1:
        raise RuntimeError('Expected exactly one directly addressed DHCP_Options record for '+dhcp_uuid)
    options[0]['_uuid']=dhcp_uuid
    return dict(port=port,bindings=binding,lsps=lsp,dhcp_options=options)


def main():
    p=argparse.ArgumentParser(); p.add_argument('port'); p.add_argument('nb'); p.add_argument('sb'); a=p.parse_args()
    print(json.dumps(evidence(a.port,a.nb,a.sb)))

if __name__=='__main__': main()
