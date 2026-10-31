"""API side isolation, saved-password binding and real CLI dispatch/exit codes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from myflames import cli, compare_api as API, compare_cli as CLI, param_compare as P, account_compare as A, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore
from test_param_compare import snapshot, item
from test_account_compare import account, side


class Isolated(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        patch=mock.patch.dict(os.environ,{'HOME':tmp.name,'USERPROFILE':tmp.name,'MYFLAMES_SECRET_BACKEND':'vault'})
        patch.start(); self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE',None)
        self.store=WorkspaceStore()
        self.temp=Path(tmp.name)


class ApiTest(Isolated):
    def test_local_snapshot_crud_and_ui_dispatch(self):
        workspace=mock.Mock(store=self.store)
        with mock.patch.object(API,'read_live',return_value=snapshot([item('x','1')])):
            saved=ui.api_response('/api/config/snapshots/save',{'connection':{},'label':'test'},workspace)
        self.assertEqual(saved['label'],'test')
        got=ui.api_response('/api/config/snapshots/get',{'id':saved['id']},workspace)
        self.assertEqual(got['snapshot']['items'][0]['value'],'1')
        self.assertEqual(len(ui.api_response('/api/config/snapshots/list',{},workspace)['items']),1)
        self.assertTrue(ui.api_response('/api/config/snapshots/delete',{'id':saved['id']},workspace)['deleted'])
        for path in API.PATHS:
            self.assertIn(path,ui._COMPARE_PATHS)
        with self.assertRaises(ValueError):
            API.response('/api/config/snapshots/get',{'id':'missing'},self.store)

    def test_saved_password_resolved_separately_per_side(self):
        seen=[]
        def fill(value):
            connection=dict(value['connection'])
            seen.append(connection['host'])
            connection['password']='secret-'+connection['host']
            return {'connection':connection}
        def capture(conn,server):
            return snapshot([item('x','1')],host=server['host'])
        with mock.patch.object(API,'MySQLConnection') as connect, mock.patch.object(P,'capture',side_effect=capture):
            report=API.response('/api/config/compare',{
              'source':{'live':{'connection':{'host':'source','user':'root'}}},
              'target':{'live':{'connection':{'host':'target','user':'root'}}}},self.store,fill)
        self.assertEqual(seen,['source','target'])
        self.assertEqual([c[1]['password'] for c in connect.call_args_list],['secret-source','secret-target'])
        self.assertEqual(report['source']['server']['host'],'source')
        self.assertEqual(report['target']['server']['host'],'target')
        self.assertNotIn('secret-source',json.dumps(report))
        self.assertNotIn('secret-target',json.dumps(report))

    def test_saved_profile_and_error_redaction_with_other_side_retained(self):
        # Actual profile storage and field validation, not a fabricated profile lookup.
        profile=self.store.save('profile',{'name':'prod','host':'source','port':3306,'user':'admin'})
        saved=self.store.parameter_snapshot_save(snapshot([item('x','1')]))
        with mock.patch.object(API,'MySQLConnection',side_effect=ConnectorError('denied password-needle')):
            report=API.response('/api/config/compare',{'source':{'profile':'prod'},'target':{'snapshot':saved['id']}},
               self.store,lambda value:{'connection':dict(value['connection'],password='password-needle')})
        self.assertEqual(report['items'][0]['status'],'not_compared')
        self.assertEqual(report['items'][0]['target']['value'],'1')
        self.assertIn('[redacted]',report['source']['error'])
        self.assertNotIn('password-needle',json.dumps(report))
        self.assertEqual(API.profile_connection(self.store,'prod')['profile_id'],profile['id'])

    def test_latest_scoped_to_other_side_endpoint_and_file_contents_only(self):
        a=self.store.parameter_snapshot_save(snapshot([item('x','1')]))
        self.store.parameter_snapshot_save(snapshot([item('x','99')],host='unrelated'))
        report=API.response('/api/config/compare',{'source':{'snapshot':'latest'},'target':{'file':json.dumps(snapshot([item('x','1')]))}},self.store)
        self.assertEqual(report['source']['id'],a['id'])
        self.assertTrue(report['identical'])
        with self.assertRaises(ValueError):
            API.side({'file':'/etc/passwd'},self.store,None)
        with self.assertRaises(ValueError):
            API.side({'snapshot':a['id']},self.store,None,'accounts')
        with self.assertRaises(ValueError):
            API.read_pair({'snapshot':'latest'},{'snapshot':'latest'},self.store,None)

    def test_accounts_compare_and_export_through_ui(self):
        report=side([account('app',grants=['GRANT SELECT ON *.* TO `app`@`%`'])])
        workspace=mock.Mock(store=self.store)
        with mock.patch.object(API,'read_live',return_value=report):
            result=ui.api_response('/api/accounts/compare',{'source':{'live':{'connection':{}}},'target':{'live':{'connection':{}}}},workspace)
            export=ui.api_response('/api/accounts/export',{'side':{'live':{'connection':{}}},'accounts':['app@%']},workspace)
        self.assertEqual(result['schema_version'],'account-compare-1.0')
        self.assertIn('CREATE USER IF NOT EXISTS',export['text'])
        self.assertEqual(export['filename'],'account-grants.sql')
        self.assertNotIn('password',export['side'])

    def test_bad_requests_refused_without_connection(self):
        for path,payload in [('/api/config/snapshots/save',{'label':3}),('/api/config/snapshots/get',{'id':3}),
                             ('/api/config/snapshots/list',{'limit':False}),('/api/accounts/export',{'side':{'file':'x'}})]:
            with self.subTest(path=path),mock.patch.object(API,'MySQLConnection') as connect,self.assertRaises(ValueError):
                API.response(path,payload,self.store)
            connect.assert_not_called()
        for value in ({'live':{},'profile':'prod'},None,{'unknown':'x'}):
            with self.assertRaises(ValueError):
                API.side(value,self.store,None)


class CliTest(Isolated):
    def run_cli(self,args):
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(sys,'argv',['myflames']+args),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:
                cli.main(); code=0
            except SystemExit as exc:
                code=exc.code or 0
        return code,out.getvalue(),err.getvalue()

    def test_parameter_compare_json_fail_on_and_files(self):
        a=self.store.parameter_snapshot_save(snapshot([item('x','1')]))
        b=self.store.parameter_snapshot_save(snapshot([item('x','2')]))
        args=['config','compare','snapshot:'+a['id'],'snapshot:'+b['id'],'--json']
        code,out,err=self.run_cli(args)
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['counts']['different'],1)
        code,out,err=self.run_cli(args+['--fail-on','different'])
        self.assertEqual(code,1)
        code,out,err=self.run_cli(['config','compare','snapshot:'+a['id'],'snapshot:'+a['id'],'--fail-on','different'])
        self.assertEqual(code,0,err)
        path=self.temp/'snapshot.json';path.write_text(json.dumps(snapshot([item('x','1')])))
        code,out,err=self.run_cli(['config','compare',str(path),'snapshot:'+a['id'],'--json'])
        self.assertEqual(code,0,err)
        self.assertTrue(json.loads(out)['identical'])
        destination=self.temp/'out.json'
        code,out,err=self.run_cli(args+['-o',str(destination)])
        self.assertEqual(code,0,err);self.assertEqual(out,'')
        self.assertEqual(json.loads(destination.read_text())['schema_version'],'parameter-compare-1.0')

    def test_parameter_file_read_is_bounded_in_bytes_before_decoding(self):
        path = self.temp / 'oversized.json'
        path.write_bytes(('é' * 3000).encode('utf-8') + b'\xff')
        saved = self.store.parameter_snapshot_save(snapshot([item('x', '1')]))
        real_open = open
        handles = []
        def tracked_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            spy = mock.MagicMock(wraps=handle)
            spy.__enter__.return_value = spy
            spy.__exit__.side_effect = lambda *unused: handle.close()
            handles.append(spy)
            return spy
        with mock.patch.object(P, 'MAX_FILE_BYTES', 4096), \
                mock.patch.object(CLI, 'open', side_effect=tracked_open, create=True), \
                mock.patch.object(CLI, 'MySQLConnection') as connect:
            code, out, err = self.run_cli(['config', 'compare', str(path), 'snapshot:' + saved['id'], '--json'])
        self.assertEqual(code, 2, (out, err))
        self.assertIn('file of at most 8 MiB', out + err)
        self.assertEqual(json.loads(out)['items'][0]['target']['value'], '1')
        self.assertTrue(handles)
        for handle in handles:
            handle.read.assert_called_once_with(4097)
        connect.assert_not_called()

    def test_parameter_file_read_decodes_an_exact_byte_limit(self):
        text = '{"note":"é"}'
        data = text.encode('utf-8')
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = data
        sources = CLI.Sources(mock.Mock(), 'config')
        with mock.patch.object(CLI, 'open', return_value=handle, create=True) as opened, \
                mock.patch.object(P, 'MAX_FILE_BYTES', len(data)), \
                mock.patch.object(API, 'parse_file', return_value={'validated': True}) as parse:
            value, meta = sources.side('file:snapshot.json')
        self.assertEqual(value, {'validated': True})
        self.assertEqual(meta['kind'], 'file')
        opened.assert_called_once_with('snapshot.json', 'rb')
        handle.__enter__.return_value.read.assert_called_once_with(len(data) + 1)
        parse.assert_called_once_with(text)

    def test_unreadable_sides_exit_2_keep_available_data_and_redact(self):
        a=self.store.parameter_snapshot_save(snapshot([item('x','1')]))
        with mock.patch.object(CLI,'MySQLConnection',side_effect=ConnectorError('denied secret-needle')):
            code,out,err=self.run_cli(['config','compare','live','snapshot:'+a['id'],'-psecret-needle','--json','--fail-on','different'])
        self.assertEqual(code,2)
        report=json.loads(out)
        self.assertEqual(report['items'][0]['status'],'not_compared')
        self.assertNotIn('secret-needle',out+err)
        code,out,err=self.run_cli(['config','compare','live','live'])
        self.assertEqual(code,2);self.assertEqual(out,'')
        code,out,err=self.run_cli(['config','compare','missing.json','bad.json','--json'])
        self.assertEqual(code,2)
        self.assertEqual(len(json.loads(out)['notes']),2)

    def test_a_redacted_secret_alone_does_not_fail_the_run(self):
        # report_password exists on every MySQL and is redacted on purpose:
        # an otherwise complete comparison exits 0, not 2.
        a=self.store.parameter_snapshot_save(snapshot([item('x','1'),item('report_password','one')]))
        b=self.store.parameter_snapshot_save(snapshot([item('x','1'),item('report_password','two')]))
        code,out,err=self.run_cli(['config','compare','snapshot:'+a['id'],'snapshot:'+b['id'],'--json'])
        report=json.loads(out)
        self.assertEqual({r['name']:r['status'] for r in report['items']},{'report_password':'not_compared','x':'identical'})
        self.assertEqual(code,0,(out,err))
        self.assertNotIn('one',[r['source']['value'] for r in report['items'] if r['name']=='report_password'])

    def test_a_value_redacted_for_its_shape_still_fails_the_run(self):
        # Two different cipher lists look like credentials (a:b) and are
        # redacted, so they are not known to be equal: incomplete, exit 2.
        a=self.store.parameter_snapshot_save(snapshot([item('tls_ciphersuites','TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384')]))
        b=self.store.parameter_snapshot_save(snapshot([item('tls_ciphersuites','TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256')]))
        code,out,err=self.run_cli(['config','compare','snapshot:'+a['id'],'snapshot:'+b['id'],'--json','--fail-on','different'])
        self.assertEqual(json.loads(out)['items'][0]['status'],'not_compared')
        self.assertEqual(code,2,(out,err))

    def test_snapshot_save_list_show_delete(self):
        with mock.patch.object(CLI.Sources,'capture',return_value=snapshot([item('x','1')])):
            code,out,err=self.run_cli(['config','snapshot','save','--label','first','--json'])
        self.assertEqual(code,0,err);saved=json.loads(out)
        self.assertEqual(saved['label'],'first')
        code,out,err=self.run_cli(['config','snapshot','list','--json'])
        self.assertEqual(code,0,err);self.assertEqual(len(json.loads(out)['items']),1)
        code,out,err=self.run_cli(['config','snapshot','show',saved['id'],'--json'])
        self.assertEqual(code,0,err);self.assertEqual(json.loads(out)['schema_version'],'parameter-snapshot-1.0')
        code,out,err=self.run_cli(['config','snapshot','delete',saved['id']])
        self.assertEqual(code,2);self.assertIsNotNone(self.store.parameter_snapshot_get(saved['id']))
        code,out,err=self.run_cli(['config','snapshot','delete',saved['id'],'--yes'])
        self.assertEqual(code,0,err);self.assertIsNone(self.store.parameter_snapshot_get(saved['id']))

    def test_accounts_compare_exit_codes_and_export(self):
        a,b=side([account()]),side([account(roles=[{'name':'r','host':'%','admin_option':False}])])
        with mock.patch.object(CLI.Sources,'side',side_effect=[(a,{'kind':'live'}),(b,{'kind':'live'})]):
            code,out,err=self.run_cli(['accounts','compare','live','profile:target','--json','--fail-on','different'])
        self.assertEqual(code,1,err);self.assertEqual(json.loads(out)['counts']['different'],1)
        with mock.patch.object(CLI.Sources,'side',return_value=(a,{'kind':'live'})):
            code,out,err=self.run_cli(['accounts','compare','live','profile:target','--json'])
        self.assertEqual(code,0,err)
        destination=self.temp/'grants.sql'
        with mock.patch.object(CLI.Sources,'side',return_value=(a,{'kind':'live'})):
            code,out,err=self.run_cli(['accounts','export','live','--account','app@%','-o',str(destination)])
        self.assertEqual(code,0,err);self.assertIn('CREATE USER IF NOT EXISTS',destination.read_text())
        with mock.patch.object(CLI.Sources,'side',return_value=(side([account(error='denied')]),{'kind':'live'})):
            code,out,err=self.run_cli(['accounts','compare','live','profile:target','--json'])
        self.assertEqual(code,2)

    def test_usage_errors_help_and_output_preservation(self):
        for args in (['config','snapshot'],['config','compare','live'],['accounts','export','live','--bad'],
                     ['accounts','compare','live','profile:x','--fail-on','any']):
            code,out,err=self.run_cli(args)
            self.assertEqual(code,2,(args,out,err));self.assertEqual(out,'')
        for args in (['config','compare','--help'],['accounts','compare','--help'],['accounts','export','--help'],
                     ['config','snapshot','--help'],['config','snapshot','save','--help']):
            code,out,err=self.run_cli(args)
            self.assertEqual(code,0,err);self.assertIn('Exit codes: 0',out)
        for args in (['config','--help'], ['accounts','--help']):
            code,out,err=self.run_cli(args)
            self.assertEqual(code,0,err)
            self.assertIn('compare',out)
        path=self.temp/'keep.sql';path.write_text('keep')
        code,out,err=self.run_cli(['accounts','export','snapshot:bad','-o',str(path)])
        self.assertEqual(code,2);self.assertEqual(path.read_text(),'keep')
