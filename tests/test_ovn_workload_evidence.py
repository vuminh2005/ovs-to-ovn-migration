"""Exact read-only OVN DHCP reference lookup and useful failure diagnostics."""
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_validation import v
import ovn_workload_evidence as e


PORT='4687eb0a-ce7a-4f3a-a250-eeb390d681f0'
DHCP='6a481846-24ef-4cf1-82ae-09fd3a429cc0'
OTHER='611336c2-2bb6-4425-9859-d1b4f9c91200'


class OvnEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.binding=dict(headings=['logical_port','chassis','up'],
                          data=[[PORT,['uuid',OTHER],['set',[True]]]])
        self.lsp=dict(headings=['name','dhcpv4_options'],data=[[PORT,['uuid',DHCP]]])
        self.dhcp=dict(headings=['cidr','external_ids','options'],data=[['10.231.1.0/24',
            ['map',[['subnet_id','subnet'],['port_id',PORT]]],
            ['map',[['mtu','1442'],['classless_static_route','{169.254.169.254/32,10.231.1.3}']]]]])

    def collect(self):
        with patch.object(e.subprocess,'check_output',side_effect=[json.dumps(r) for r in
                         (self.binding,self.lsp,self.dhcp)]) as run:
            result=e.evidence(PORT,'tcp:nb:6641','tcp:sb:6642')
        return result,run

    def test_exact_reference_uses_direct_record_and_preserves_evidence_schema(self):
        result,run=self.collect()
        command=run.call_args_list[2].args[0]
        self.assertEqual(command,['docker','exec','ovn_northd','ovn-nbctl','--timeout=10',
            '--db=tcp:nb:6641','--format=json','--columns=cidr,external_ids,options',
            'list','DHCP_Options',DHCP])
        self.assertFalse(any('_uuid' in arg for arg in command))
        self.assertEqual(result['dhcp_options'][0]['_uuid'],DHCP)
        self.assertEqual(result['dhcp_options'][0]['cidr'],'10.231.1.0/24')
        self.assertEqual(result['dhcp_options'][0]['external_ids'],{'subnet_id':'subnet','port_id':PORT})
        self.assertEqual(result['dhcp_options'][0]['options']['mtu'],'1442')
        self.assertEqual(result['lsps'],[dict(name=PORT,dhcpv4_options=DHCP)])
        self.assertEqual(result['port'],PORT)

    def test_singleton_set_uuid_reference_is_supported(self):
        self.lsp['data'][0][1]=['set',[['uuid',DHCP]]]
        result,_=self.collect()
        self.assertEqual(result['dhcp_options'][0]['_uuid'],DHCP)

    def test_sb_port_binding_query_and_healthy_evidence_are_unchanged(self):
        result,run=self.collect()
        self.assertEqual(run.call_args_list[0].args[0],['docker','exec','ovn_northd','ovn-sbctl',
            '--timeout=10','--db=tcp:sb:6642','--format=json','--columns=logical_port,chassis,up',
            'find','Port_Binding','logical_port='+PORT])
        self.assertEqual(result['bindings'],[dict(logical_port=PORT,chassis=OTHER,up=[True])])

    def test_zero_multiple_or_malformed_dhcp_references_fail_before_direct_query(self):
        for refs in (['set',[]],['set',[['uuid',DHCP],['uuid',OTHER]]],
                     ['uuid','not-a-uuid'],['set',[['uuid',DHCP],['uuid',DHCP]]],
                     None,['set',[{'unexpected':'reference'}]]):
            with self.subTest(refs=refs):
                self.lsp['data'][0][1]=refs
                with patch.object(e.subprocess,'check_output',side_effect=[json.dumps(self.binding),json.dumps(self.lsp)]) as run:
                    with self.assertRaises(RuntimeError):
                        e.evidence(PORT,'nb','sb')
                    self.assertEqual(run.call_count,2)

    def test_missing_multiple_or_wrong_lsp_fails_before_direct_query(self):
        for rows in ([],[self.lsp['data'][0],self.lsp['data'][0]],[[OTHER,['uuid',DHCP]]]):
            with self.subTest(rows=rows):
                lsp=dict(self.lsp,data=rows)
                with patch.object(e.subprocess,'check_output',side_effect=[json.dumps(self.binding),json.dumps(lsp)]) as run:
                    with self.assertRaisesRegex(RuntimeError,'exactly one Logical_Switch_Port'):
                        e.evidence(PORT,'nb','sb')
                    self.assertEqual(run.call_count,2)

    def test_missing_or_multiple_direct_records_fail_safely(self):
        record=self.dhcp['data'][0]
        for rows in ([],[record,record]):
            with self.subTest(rows=rows):
                self.dhcp['data']=rows
                with self.assertRaisesRegex(RuntimeError,'exactly one directly addressed DHCP_Options'):
                    self.collect()

    def test_malformed_json_and_invalid_table_shapes_fail_safely(self):
        malformed=('not json','[]','{}',json.dumps(dict(headings=['cidr','external_ids','options'],data=[[]])),
                   json.dumps(dict(headings=['cidr','cidr','options'],data=[['cidr',[],[]]])))
        for output in malformed:
            with self.subTest(output=output),patch.object(e.subprocess,'check_output',
                    side_effect=[json.dumps(self.binding),json.dumps(self.lsp),output]):
                with self.assertRaisesRegex(RuntimeError,'ovn-nbctl list DHCP_Options returned malformed OVN JSON'):
                    e.evidence(PORT,'nb','sb')

    def test_failed_direct_query_preserves_tool_table_returncode_stderr_and_stdout(self):
        failure=subprocess.CalledProcessError(2,['ovn-nbctl'],output='query output',
            stderr='ovn-nbctl: DHCP_Options record does not exist')
        with patch.object(e.subprocess,'check_output',side_effect=[json.dumps(self.binding),json.dumps(self.lsp),failure]) as run:
            with self.assertRaises(RuntimeError) as raised:
                e.evidence(PORT,'database-connection-not-for-diagnostics','sb')
        message=str(raised.exception)
        for expected in ('ovn-nbctl list DHCP_Options','return code 2','record does not exist','query output'):
            self.assertIn(expected,message)
        self.assertNotIn('database-connection-not-for-diagnostics',message)
        self.assertEqual(run.call_args.kwargs['stderr'],subprocess.PIPE)

    def test_missing_direct_record_command_failure_fails_safely(self):
        with patch.object(e.subprocess,'check_output',side_effect=[json.dumps(self.binding),json.dumps(self.lsp),
                    subprocess.CalledProcessError(1,['ovn-nbctl'],stderr='no row '+DHCP)]):
            with self.assertRaisesRegex(RuntimeError,'no row '+DHCP):
                e.evidence(PORT,'nb','sb')

    def test_outer_ansible_failure_preserves_inner_ovn_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj=v.Validation.__new__(v.Validation)
            obj.root=pathlib.Path(tmp)
            obj.cfg=dict(inventory='inventory',ovn_nb='nb',ovn_sb='sb',ovn_cli_host='controller')
            obj.state={'pre':{'existing':{'0':{'port':PORT}}}}
            failure=subprocess.CalledProcessError(2,['ansible-playbook'],
                output='ovn-nbctl list DHCP_Options failed with return code 1; stderr: no row',stderr='helper failed')
            with patch.object(v.subprocess,'run',side_effect=failure):
                with self.assertRaises(RuntimeError) as raised:
                    obj.ovn_evidence('0')
            message=str(raised.exception)
            for expected in ('workload-ovn-evidence-tasks.yml','return code 2','helper failed','no row','DHCP_Options'):
                self.assertIn(expected,message)


if __name__=='__main__':
    unittest.main()
