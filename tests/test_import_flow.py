"""Exercise the actual importer orchestration with offline recorder/API doubles.
This does not assert Home Assistant's real recorder integration is validated.
"""
import ast
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import unittest
try:
    from tests.test_reconcile import m, hour, row, T
except ModuleNotFoundError:
    from test_reconcile import m, hour, row, T

SOURCE=Path(__file__).resolve().parents[1]/'custom_components/glowmarkt/history.py'
NAMES={'async_import_history_statistics','_existing_statistic_rows',
       '_fetch_half_hour_history','_history_window_end','_useful_history_end',
       '_floor_to_half_hour'}

def namespace():
    tree=ast.parse(SOURCE.read_text())
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+
        [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in NAMES],type_ignores=[])
    ns=dict(datetime=datetime,timedelta=timedelta,timezone=timezone,partial=partial,
        reconcile_statistics=m.reconcile_statistics,IncompleteHistory=m.IncompleteHistory,
        _INITIAL_HISTORY_DAYS=400,_RECENT_HISTORY_DAYS=7,_HISTORY_CHUNK_DAYS=10,
        _FULL_REFRESH_INTERVAL=timedelta(days=1),_READINGS_END_EPSILON=timedelta(seconds=1),
        _LOGGER=logging.getLogger('flow-test'))
    exec(compile(ast.fix_missing_locations(module),str(SOURCE),'exec'),ns)
    return ns

class FakeHass:
    async def async_add_executor_job(self,fn,*args):return fn(*args)

class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns=namespace();self.writes=[];self.fetch_windows=[];self.query_fails=False
        self.old=[row(T,2),row(T+m.HOUR,5),row(T+2*m.HOUR,9)]
        self.data=hour(T,4)
        resource=SimpleNamespace(id='synthetic')
        self.target=SimpleNamespace(statistic_id='glowmarkt:test',resource=resource,
            dcc_context_resource=None,value_scale=1)
        self.end=T+3*m.HOUR
        self.ns['_history_window_end']=lambda:self.end
        self.ns['_select_history_targets']=lambda ve,rs:[self.target]
        self.ns['_history_metadata']=lambda target:{'statistic_id':target.statistic_id}
        async def noop(*args):pass
        async def executor(hass,job):return job()
        async def target_end(*args):return self.end
        self.ns['_async_trigger_catchup']=noop
        self.ns['_async_recorder_executor_job']=executor
        self.ns['_async_target_history_end']=target_end
        self.client=SimpleNamespace(list_virtual_entities=lambda:[SimpleNamespace(list_resources=lambda:[])])
        def fetch(resource,start,end):
            self.fetch_windows.append((start,end));return self.data
        def query(*args):
            if self.query_fails:raise RuntimeError('simulated recorder query failure')
            self.assertEqual(args[1],datetime(1970,1,1,tzinfo=timezone.utc))
            return {'glowmarkt:test':self.old}
        def publish(hass,meta,rows):self.writes.append((meta,rows))
        self.ns['_fetch_half_hour_history']=fetch
        self.ns['_recorder_statistics_api']=lambda:(publish,lambda *a,**kw:{'glowmarkt:test':{}},query)
    async def run_import(self,state=None):
        with self.assertLogs('flow-test',level='DEBUG'):
            await self.ns['async_import_history_statistics'](FakeHass(),self.client,state)
    async def test_updates_later_tail_and_only_target_id(self):
        await self.run_import({})
        self.assertEqual(self.writes[0][0]['statistic_id'],'glowmarkt:test')
        self.assertEqual([r['sum'] for r in self.writes[0][1]],[4,7,11])
    async def test_query_failure_never_publishes_zero_baseline(self):
        self.query_fails=True
        await self.run_import({})
        self.assertFalse(self.writes)
    async def test_unreconstructable_tail_never_publishes_prefix(self):
        self.old=[row(T,124.575),row(T+m.HOUR,39.848)]
        await self.run_import({})
        self.assertFalse(self.writes)
    async def test_daily_full_and_recent_refresh_schedule(self):
        state={}
        await self.run_import(state)
        self.assertEqual((self.fetch_windows[-1][1]-self.fetch_windows[-1][0]).days,400)
        await self.run_import(state)
        self.assertEqual((self.fetch_windows[-1][1]-self.fetch_windows[-1][0]).days,7)
        self.end+=timedelta(days=1)
        await self.run_import(state)
        self.assertEqual((self.fetch_windows[-1][1]-self.fetch_windows[-1][0]).days,400)
    async def test_failed_reconciliation_still_limits_full_sweep_frequency(self):
        self.old=[row(T,10),row(T+m.HOUR,0)]
        state={}
        await self.run_import(state);await self.run_import(state)
        self.assertEqual((self.fetch_windows[-1][1]-self.fetch_windows[-1][0]).days,7)
        self.assertFalse(self.writes)

class FetchTests(unittest.TestCase):
    def test_end_exclusive_and_chunk_deduplication(self):
        ns=namespace();calls=[]
        end=T+timedelta(days=11)
        def readings(resource,start,stop,*args):
            calls.append((start,stop))
            # Include out-of-range values to verify bounds filtering.
            return hour(start,2)+hour(end,9)
        ns['get_resource_readings']=readings
        out=ns['_fetch_half_hour_history'](None,T,end)
        self.assertEqual(len(calls),2)
        self.assertEqual(len(out),4)
        self.assertTrue(all(T<=t<end for t,v in out))
    def test_useful_end_excludes_partial_last_hour(self):
        ns=namespace()
        self.assertEqual(ns['_useful_history_end'](T+3*m.HOUR,T+m.HOUR),T+m.HOUR)
        self.assertEqual(ns['_useful_history_end'](T+3*m.HOUR,T+m.HOUR+timedelta(minutes=30)),T+2*m.HOUR)

if __name__=='__main__':unittest.main()
