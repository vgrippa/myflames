"""Release checking uses recorded responses and never contacts a live vendor."""
import copy
import datetime
import json
import threading
import time
import unittest
from unittest import mock
from myflames import tool_updates as tu


def tool(name='mysql', version=(8, 4, 10), engine='mysql', **extra):
    entry = dict(available=True, engine=engine, version_tuple=list(version), version='%s %s' % (name, '.'.join(map(str, version))))
    entry.update(extra)
    return {'tools': {name: entry}}


class ToolUpdates(unittest.TestCase):
    def setUp(self):
        with tu._LOCK:
            tu._CACHE.clear()
        self.assertFalse(tu._INFLIGHT)

    def check(self, report, latest=(8, 4, 11)):
        with mock.patch.object(tu, '_release', return_value=latest):
            return tu.enrich_tools(report, True)['tools']

    def test_default_has_links_and_no_network(self):
        with mock.patch.object(tu, '_fetch', side_effect=AssertionError('network')):
            result = tu.enrich_tools(tool())['tools']['mysql']
        self.assertEqual(result['download_url'], tu.MYSQL_DOWNLOAD)
        self.assertEqual(result['update']['status'], 'not_checked')

    def test_statuses(self):
        for installed, expected in [((8,4,10),'available'), ((8,4,11),'current'), ((8,4,12),'unknown')]:
            with self.subTest(installed=installed):
                result = self.check(tool(version=installed))['mysql']['update']
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['latest_version'], '8.4.11')
                self.assertTrue(result['checked_at'].endswith('Z'))

    def test_missing_broken_deprecated_and_alias(self):
        report = {'tools': {'mysql': {'available':False}, 'mysqldump': {'available':False, 'path':'/fake/dump'},
                            'mysqlpump': {'available':True, 'deprecated':True}}}
        with mock.patch.object(tu, '_release', side_effect=AssertionError('network')):
            result = tu.enrich_tools(report, True)['tools']
        self.assertEqual(result['mysql']['update']['status'],'not_installed')
        self.assertEqual(result['mysqldump']['update']['status'],'unknown')
        self.assertEqual(result['mysqlpump']['update']['status'],'deprecated')
        for name in ('mysql', 'mysqldump'):
            result = self.check(tool(name,(11,4,12),'mariadb'),(11,4,13))[name]
            self.assertEqual(result['download_url'],tu.MARIA_DOWNLOAD)
            self.assertEqual(result['update']['status'],'available')

    def test_unsupported_banners(self):
        for banner in ('Percona 8.4.10','MySQL 8.4.10-commercial','MySQL 8.4.10-rc', 'MySQL 8.4.10-trunk'):
            with mock.patch.object(tu, '_release', side_effect=AssertionError('network')):
                report=tool(); report['tools']['mysql']['version']=banner
                self.assertEqual(tu.enrich_tools(report,True)['tools']['mysql']['update']['status'],'unknown')

    def test_offline_failure_is_cached_then_expires(self):
        with mock.patch.object(tu,'_release',side_effect=OSError('offline')) as fetch:
            for _ in range(2):
                self.assertEqual(tu.enrich_tools(tool(),True)['tools']['mysql']['update']['status'],'unknown')
            self.assertEqual(fetch.call_count,1)
            with tu._LOCK:
                key=('mysql','8.4'); tu._CACHE[key]=(0,tu._CACHE[key][1])
            tu.enrich_tools(tool(),True)
            self.assertEqual(fetch.call_count,2)

    def test_partial_response_does_not_reuse_expired_success(self):
        with tu._LOCK:
            tu._CACHE[('mysql', '8.4')] = (0, ((8, 4, 11), None, 'old'))
        with mock.patch.object(tu, '_release', side_effect=tu.http.client.IncompleteRead(b'partial')):
            update = tu.enrich_tools(tool(), True)['tools']['mysql']['update']
        self.assertEqual(update['status'], 'unknown')
        self.assertIsNone(update['latest_version'])

    def test_mysql_strict_ga_and_series(self):
        for body, good in [('<h1>MySQL Community Server 8.4.11 LTS </h1> General Availability (GA) Releases',True),
                           ('<h1>MySQL Community Server 9.7.2</h1> General Availability (GA) Releases',False),
                           ('<h1>MySQL Community Server 8.4.11-rc</h1> General Availability (GA) Releases',False),
                           ('<h1>MySQL Community Server 8.4.11</h1>',False)]:
            with mock.patch.object(tu,'_fetch',return_value=body):
                if good:self.assertEqual(tu._release('mysql','8.4'),(8,4,11))
                else:
                    with self.assertRaises(ValueError):tu._release('mysql','8.4')
        with mock.patch.object(tu,'_fetch',return_value='<h1>MySQL Shell 26.7.1</h1>General Availability (GA) Releases'):
            self.assertEqual(tu._release('shell',''),(26,7,1))

    def test_mariadb_stability_release_date_files_and_series(self):
        catalog={'major_releases':[{'release_id':'11.4','release_status':'Stable'}]}
        release={'releases':{'11.4.13':{'release_id':'11.4.13','date_of_release':'2026-01-01','files':[{}]}}}
        with mock.patch.object(tu,'_fetch',side_effect=[json.dumps(catalog),json.dumps(release)]):
            self.assertEqual(tu._release('mariadb','11.4'),(11,4,13))
        variants=[]
        for field,value in [('release_id','11.8.2'),('date_of_release','9999-01-01'),('date_of_release','2026-1-1'),('files',[])]:
            variant=copy.deepcopy(release);variant['releases']['11.4.13'][field]=value;variants.append(variant)
        variants.extend([{'releases':{'11.4.13-rc':{}}},{'releases':[]}])
        for variant in variants:
            with mock.patch.object(tu,'_fetch',side_effect=[json.dumps(catalog),json.dumps(variant)]):
                with self.assertRaises(ValueError):tu._release('mariadb','11.4')
        catalog['major_releases'][0]['release_status']='RC'
        with mock.patch.object(tu,'_fetch',return_value=json.dumps(catalog)) as fetch:
            with self.assertRaises(ValueError):tu._release('mariadb','11.4')
            self.assertEqual(fetch.call_count,1)

    def test_http_guardrails(self):
        for url in ('http://dev.mysql.com/downloads/shell/','https://evil.example/','file:///tmp/file',tu.MYSQL_DOWNLOAD+'?version=8.4&url=x'):
            with self.assertRaises(ValueError):tu._fetch(url)
        with self.assertRaises(ValueError):tu._NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example/')
        response=mock.MagicMock();response.__enter__.return_value=response;response.status=200;response.geturl.return_value=tu.SHELL_DOWNLOAD
        response.read.return_value=b'x'*(tu.MAX_BYTES+1)
        with mock.patch.object(tu.urllib.request,'build_opener') as build:
            build.return_value.open.return_value=response
            with self.assertRaises(ValueError):tu._fetch(tu.SHELL_DOWNLOAD)
            self.assertEqual(build.call_args[0][0].proxies,{})
            self.assertEqual(build.return_value.open.call_args[1]['timeout'],5)
        response.read.return_value=b'ok';response.geturl.return_value='https://evil.example/'
        with mock.patch.object(tu.urllib.request,'build_opener') as build:
            build.return_value.open.return_value=response
            with self.assertRaises(ValueError):tu._fetch(tu.SHELL_DOWNLOAD)

    def test_concurrent_calls_share_one_fetch(self):
        entered=threading.Event();finish=threading.Event();results=[]
        def fetch(*args):entered.set();finish.wait(2);return (8,4,11)
        with mock.patch.object(tu,'_release',side_effect=fetch) as request:
            threads=[threading.Thread(target=lambda:results.append(tu.enrich_tools(tool(),True))) for _ in range(5)]
            for thread in threads:thread.start()
            self.assertTrue(entered.wait(1));finish.set()
            for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())
            self.assertEqual(request.call_count,1)
        self.assertEqual(len(results),5)

    def test_total_wait_is_bounded_even_when_network_stalls(self):
        finish=threading.Event()
        def fetch(*args):finish.wait(2);return (8,4,11)
        try:
            with mock.patch.object(tu,'_release',side_effect=fetch),mock.patch.object(tu,'CHECK_TIMEOUT',0.03):
                start=time.monotonic();result=tu.enrich_tools(tool(),True)
                self.assertLess(time.monotonic()-start,0.3)
                self.assertEqual(result['tools']['mysql']['update']['status'],'unknown')
        finally:
            finish.set()
            for _ in range(100):
                if not tu._INFLIGHT:break
                time.sleep(0.01)

    def test_cache_size_and_worker_limit(self):
        for i in range(tu.MAX_CACHE):tu._CACHE[('old',str(i))]=(0,(None,None,None))
        self.check(tool());self.assertEqual(len(tu._CACHE),tu.MAX_CACHE)
        with tu._LOCK:
            tu._CACHE.clear()
            for i in range(tu.MAX_WORKERS):tu._INFLIGHT[('busy',str(i))]=threading.Event()
        try:
            with mock.patch.object(tu,'_release',side_effect=AssertionError('network')):
                self.assertEqual(tu.enrich_tools(tool(),True)['tools']['mysql']['update']['status'],'unknown')
        finally:
            with tu._LOCK:tu._INFLIGHT.clear()

if __name__=='__main__':unittest.main()
