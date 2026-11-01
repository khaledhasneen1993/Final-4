import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(server,'RUNTIME',Path(self.tmp.name))
        self.patch.start()
        self.assets_patch=patch.object(server,'ASSETS',Path(__file__).resolve().parents[1])
        self.assets_patch.start()
        self.supervisor = server.Supervisor()
        self.client = TestClient(TestServer(server.make_app(self.supervisor,autostart=False)))
        await self.client.start_server()
        self.host = {'Host':'127.0.0.1:8787'}

    async def asyncTearDown(self):
        await self.client.close()
        self.patch.stop()
        self.assets_patch.stop()
        self.tmp.cleanup()

    async def test_read_export_and_control_auth(self):
        s={'updated_at':time.time(),'balance_usdt':101,'equity_usdt':101,'trades':[
            dict(id='1',symbol='TEST',side='buy',amount=1,price=100,fee_usdt=.05,reduceOnly=False),
            dict(id='2',symbol='TEST',side='sell',amount=1,price=101.1,fee_usdt=.05,reduceOnly=True,pnl_usdt=1.1)]}
        (server.RUNTIME/'data/final.snapshot.json').write_text(json.dumps(s))
        r=await self.client.get('/api/status',headers=self.host)
        self.assertEqual(r.status,200)
        data=await r.json()
        self.assertAlmostEqual(data['bots']['final']['stats']['realized_net'],1)
        self.assertFalse(data['bots']['final']['live'])
        r=await self.client.get('/export/final/closed',headers=self.host)
        self.assertEqual(r.status,200)
        self.assertIn('net_pnl_usdt',await r.text())
        r=await self.client.post('/api/final/stop',headers=self.host)
        self.assertEqual(r.status,403)
        headers={**self.host,'X-Control-Token':data['token'],'Origin':'https://evil.example'}
        r=await self.client.post('/api/final/stop',headers=headers)
        self.assertEqual(r.status,403)
        headers['Origin']='http://127.0.0.1:8787'
        r=await self.client.post('/api/final/stop',headers=headers)
        self.assertEqual(r.status,200)
        r=await self.client.get('/api/status',headers={'Host':'evil.example'})
        self.assertEqual(r.status,403)

    async def test_duplicate_start_stop(self):
        class FakeProcess:
            returncode=None
            stdout=None
            def send_signal(self,sig):self.returncode=0
            async def wait(self):return self.returncode
        async def no_logs(*args):pass
        with patch('asyncio.create_subprocess_exec',return_value=FakeProcess()) as spawn:
            with patch.object(self.supervisor,'read_logs',no_logs):
                await self.supervisor.start('final')
                await self.supervisor.start('final')
                self.assertEqual(spawn.call_count,1)
                self.assertEqual(spawn.call_args.kwargs['cwd'],server.ASSETS)
                await self.supervisor.stop('final')
                self.assertEqual(self.supervisor.procs['final'].returncode,0)

    async def test_assets_are_served(self):
        for path in ('/','/app.js','/style.css'):
            r=await self.client.get(path,headers=self.host)
            self.assertEqual(r.status,200,path)
            self.assertGreater(len(await r.read()),100)

    async def test_per_symbol_freshness_expires_at_response_time(self):
        class Process:
            returncode=None
        now=time.time()
        self.supervisor.procs['final']=Process()
        self.supervisor.started['final']=now-20
        state=dict(updated_at=now-4,scanner=dict(connected=True,last_event_age=1,
            symbols=['BTC','ETH'],symbol_health=[dict(symbol='BTC',age=1,event_age=1,fresh=True),
                                              dict(symbol='ETH',age=7,event_age=7,fresh=True)]))
        (server.RUNTIME/'data/final.snapshot.json').write_text(json.dumps(state))
        result=self.supervisor.snapshot('final')
        self.assertTrue(result['live'])
        self.assertEqual(result['scanner']['fresh_symbols'],1)
        self.assertFalse(result['scanner']['symbol_health'][1]['fresh'])
        self.supervisor.procs.clear()

if __name__=='__main__':unittest.main()


class PairTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = DashboardTests.asyncSetUp
    asyncTearDown = DashboardTests.asyncTearDown
    async def test_pair_and_final4_export(self):
        self.assertEqual(server.NAMES, ('final', 'final4'))
        state = dict(updated_at=time.time(), balance_usdt=20.5, equity_usdt=20.5,
            settings={'STOP_LOSS_PCT': .07}, trades=[])
        (server.RUNTIME/'data/final4.snapshot.json').write_text(json.dumps(state))
        response = await self.client.get('/api/status', headers=self.host)
        payload = await response.json()
        self.assertEqual(set(payload['bots']), {'final', 'final4'})
        self.assertEqual(payload['bots']['final4']['source']['repository'], 'Final-4')
        self.assertEqual(payload['bots']['final4']['balance_usdt'], 20.5)
        response = await self.client.get('/export/final4/snapshot', headers=self.host)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['settings']['STOP_LOSS_PCT'], .07)
        response = await self.client.get('/export/final2/snapshot', headers=self.host)
        self.assertEqual(response.status, 404)
        response = await self.client.get('/', headers=self.host)
        self.assertIn('data-bot="final4"', await response.text())
