import importlib.util
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reconcile', ROOT / 'custom_components/glowmarkt/reconcile.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
T = datetime(2026, 10, 20, tzinfo=timezone.utc)
def half(t, v): return (t, SimpleNamespace(value=v))
def hour(t, v): return [half(t, v / 2), half(t.astimezone(timezone.utc) + timedelta(minutes=30), v / 2)]
def row(t, v): return {'start': t, 'sum': v}

class ReconcileTests(unittest.TestCase):
    def test_repeat_and_overlap_are_idempotent(self):
        data = hour(T, 2) + hour(T + m.HOUR, 3)
        old = m.reconcile_statistics(data, [])
        self.assertEqual(m.reconcile_statistics(data, old), old)
        self.assertEqual(m.reconcile_statistics(hour(T + m.HOUR, 3), old), old[1:])
    def test_historical_correction_rebases_tail(self):
        old = [row(T, 2), row(T+m.HOUR, 5), row(T+2*m.HOUR, 9)]
        out = m.reconcile_statistics(hour(T, 4), old)
        self.assertEqual([r['sum'] for r in out], [4, 7, 11])
    def test_missing_baseline_gap_is_not_zero(self):
        with self.assertRaises(m.IncompleteHistory):
            m.reconcile_statistics(hour(T+13*timedelta(days=1), 2), [row(T,155.096)])
    def test_late_data_fills_gap_and_rebases_later_hours(self):
        old = [row(T, 1), row(T+3*m.HOUR, 8), row(T+4*m.HOUR, 10)]
        data = hour(T+m.HOUR, 2)+hour(T+2*m.HOUR, 3)+hour(T+3*m.HOUR, 4)
        self.assertEqual([x['sum'] for x in m.reconcile_statistics(data,old)], [3,6,10,12])
    def test_negative_tail_requires_source(self):
        old = [row(T,124.575),row(T+m.HOUR,39.848)]
        with self.assertRaises(m.IncompleteHistory):
            m.reconcile_statistics(hour(T,.218),old)
    def test_negative_tail_repaired_when_source_arrives(self):
        out=m.reconcile_statistics(hour(T,.218)+hour(T+m.HOUR,.3),[row(T,124.575),row(T+m.HOUR,39.848)])
        self.assertEqual([x['sum'] for x in out],[.218,.518])
    def test_partial_hour_preserves_existing_amount(self):
        old=[row(T,1),row(T+m.HOUR,3),row(T+2*m.HOUR,6)]
        out=m.reconcile_statistics(hour(T,2)+[half(T+m.HOUR,99)],old)
        self.assertEqual([x['sum'] for x in out],[2,4,7])
    def test_only_partial_hour_publishes_nothing(self):
        self.assertEqual(m.reconcile_statistics([half(T,1)],[]),[])
    def test_missing_internal_hour_refuses_import(self):
        with self.assertRaises(m.IncompleteHistory):
            m.reconcile_statistics(hour(T,1)+hour(T+2*m.HOUR,1),[])
    def test_duplicate_buckets_and_invalid_amounts(self):
        data=hour(T,2)
        self.assertEqual(m.complete_hour_totals(data+data),{T:2})
        for value in [-1,float('nan'),float('inf')]:
            with self.assertRaises(m.IncompleteHistory):m.complete_hour_totals([half(T,value)])
        with self.assertRaises(m.IncompleteHistory):m.complete_hour_totals(data+[half(T,4)])
    def test_cost_scaling_and_real_zero(self):
        self.assertEqual(m.reconcile_statistics(hour(T,100)+hour(T+m.HOUR,0),[],.01),[row(T,1),row(T+m.HOUR,1)])
    def test_out_of_order_and_epoch_recorder_rows(self):
        data=hour(T+m.HOUR,3)+hour(T,2)
        self.assertEqual(m.reconcile_statistics(data,[row((T-m.HOUR).timestamp(),10)]),[row(T,12),row(T+m.HOUR,15)])
    def test_autumn_repeated_hour_is_distinct(self):
        zone=ZoneInfo('Europe/London')
        first=datetime(2026,10,25,1,tzinfo=zone,fold=0)
        second=datetime(2026,10,25,1,tzinfo=zone,fold=1)
        data=hour(first,2)+hour(second,4)
        out=m.reconcile_statistics(data,[])
        self.assertEqual(len(out),2)
        self.assertEqual([x['sum'] for x in out],[2,6])
    def test_spring_missing_wallclock_hour_is_not_missing_utc(self):
        zone=ZoneInfo('Europe/London')
        first=datetime(2026,3,29,0,tzinfo=zone)
        second=datetime(2026,3,29,2,tzinfo=zone)
        self.assertEqual([x['sum'] for x in m.reconcile_statistics(hour(first,1)+hour(second,2),[])],[1,3])

if __name__=='__main__':unittest.main()
