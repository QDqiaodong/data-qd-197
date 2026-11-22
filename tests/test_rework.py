import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from recipe_yield import cli
from recipe_yield.rework import reconcile


def receipt(request_id, lines):
    return {'request_id': request_id, 'use_on': '2027-01-01', 'total_cost': '0',
            'lines': [{'order_id': order, 'path': [path], 'ingredient': 'flour',
                       'net': grams, 'gross': grams, 'trim': '0', 'cost': '0',
                       'tags': [], 'draws': [{'lot_id': lot, 'gross': grams,
                                              'cost': '0'}]}
                      for order, path, lot, grams in lines]}


def draw(request_id, lot_id, grams):
    return {'request_id': request_id, 'lot_id': lot_id, 'grams': grams}


def sub(mix_id, grams):
    return {'mix_id': mix_id, 'grams': grams}


def mix(event_id, inputs, output):
    return {'id': event_id, 'kind': 'mix', 'inputs': inputs,
            'output_grams': output}


def amend(event_id, mix_id, inputs):
    return {'id': event_id, 'kind': 'amend', 'mix_id': mix_id, 'inputs': inputs}


def pack(parcel_id, mix_id, grams, order_id='O'):
    return {'id': parcel_id, 'kind': 'pack', 'mix_id': mix_id,
            'order_id': order_id, 'grams': grams}


def ship(event_id, parcel_id):
    return {'id': event_id, 'kind': 'ship', 'parcel_id': parcel_id}


def receipts():
    return [receipt('R1', [('O', 'x:0', 'A', '4'), ('O', 'x:1', 'B', '4'),
                           ('O', 'x:2', 'C', '7')]),
            receipt('R2', [('O', 'y:0', 'A', '1')])]


def timeline():
    return [
        mix('M0', [draw('R1', 'A', '4'), draw('R1', 'C', '4')], '6'),
        mix('M1', [sub('M0', '2'), draw('R2', 'A', '1'), draw('R1', 'C', '1')], '3'),
        mix('M2', [draw('R1', 'C', '2')], '2'),
        pack('P1', 'M1', '1'),
        pack('P2', 'M0', '2'),
        pack('P3', 'M2', '2'),
        ship('S1', 'P1'),
        amend('AM1', 'M0', [draw('R1', 'B', '4'), draw('R1', 'C', '4')]),
    ]


def archive(lot_ids=('A',), events=None, compare_to=7, as_known=8):
    events = timeline() if events is None else events
    return {'receipts': receipts(), 'orders': [{'id': 'O', 'demand_grams': '5'}],
            'events': events, 'lot_ids': list(lot_ids),
            'compare_to': compare_to, 'as_known': as_known}


def by_id(rows):
    return {row['id']: row for row in rows}


class ScenarioOneTests(unittest.TestCase):
    def test_amendment_reshapes_holds_and_replenishment(self):
        report = reconcile(archive())
        before, after = report['before'], report['after']
        rows = by_id(before['parcels'])
        self.assertEqual(rows['P1']['attributed_grams'], '1/2')
        self.assertEqual(rows['P1']['action'], 'trace')
        self.assertEqual(rows['P2']['attributed_grams'], '1')
        self.assertEqual(rows['P2']['action'], 'hold')
        self.assertEqual(rows['P3']['attributed_grams'], '0')
        self.assertEqual(rows['P3']['action'], 'ready')
        rows = by_id(after['parcels'])
        self.assertEqual(rows['P1']['attributed_grams'], '1/4')
        self.assertEqual(rows['P1']['action'], 'trace')
        self.assertEqual(rows['P2']['attributed_grams'], '0')
        self.assertEqual(rows['P2']['action'], 'ready')
        self.assertEqual(rows['P3']['action'], 'ready')
        self.assertEqual(before['orders'][0],
                         {'id': 'O', 'demand_grams': '5', 'ready': '2',
                          'shipped_clear': '0', 'hold': '2', 'trace': '1',
                          'gap_grams': '3'})
        self.assertEqual(after['orders'][0],
                         {'id': 'O', 'demand_grams': '5', 'ready': '4',
                          'shipped_clear': '0', 'hold': '0', 'trace': '1',
                          'gap_grams': '1'})
        self.assertEqual(report['action_changes'],
                         [{'id': 'P2', 'before': 'hold', 'after': 'ready'}])

    def test_watching_b_flips_shipped_clear_and_gap(self):
        report = reconcile(archive(lot_ids=('B',)))
        before, after = report['before'], report['after']
        self.assertEqual(by_id(before['parcels'])['P1']['action'],
                         'shipped_clear')
        self.assertEqual(before['orders'][0]['gap_grams'], '0')
        rows = by_id(after['parcels'])
        self.assertEqual(rows['P1']['attributed_grams'], '1/4')
        self.assertEqual(rows['P1']['action'], 'trace')
        self.assertEqual(rows['P2']['attributed_grams'], '1')
        self.assertEqual(rows['P2']['action'], 'hold')
        self.assertEqual(after['orders'][0]['gap_grams'], '3')
        self.assertEqual([c['id'] for c in report['action_changes']], ['P1', 'P2'])

    def test_two_paths_into_one_mix_both_count(self):
        # Both watched lots feed M0; before the amendment all 8g of input is
        # attributable, so the whole 6g output carries the watch and P2's 2g
        # is fully attributed.  Neither path is de-duplicated.
        report = reconcile(archive(lot_ids=('A', 'C'), as_known=7))
        self.assertEqual(by_id(report['after']['parcels'])['P2']['attributed_grams'],
                         '2')

    def test_zero_attribution_parcel_is_ready_not_shipped(self):
        report = reconcile(archive(lot_ids=('C',), compare_to=0, as_known=7))
        self.assertEqual(report['before']['parcels'], [])
        self.assertEqual([c['id'] for c in report['action_changes']],
                         ['P1', 'P2', 'P3'])
        for change in report['action_changes']:
            self.assertIsNone(change['before'])

    def test_all_parcels_lists_orders_without_parcels(self):
        data = archive()
        data['orders'].append({'id': 'N', 'demand_grams': '1'})
        report = reconcile(data)
        self.assertEqual([row['id'] for row in report['after']['orders']],
                         ['N', 'O'])
        self.assertEqual(report['after']['orders'][0]['gap_grams'], '1')


class ScenarioTwoTests(unittest.TestCase):
    def amended(self):
        return timeline() + [
            amend('AM2', 'M0', [draw('R1', 'B', '3'), draw('R1', 'C', '5')]),
            amend('AM3', 'M0', [draw('R1', 'B', '4'), draw('R1', 'C', '4')])]

    def test_prefix_to_eight_still_succeeds(self):
        report = reconcile(archive(events=self.amended(), as_known=8))
        self.assertEqual(report['action_changes'],
                         [{'id': 'P2', 'before': 'hold', 'after': 'ready'}])

    def test_later_amendment_cannot_hide_intermediate_overdraw(self):
        # At step 9 M0 would draw 5g C while M1 and M2 already hold 3g of the
        # 7g C credit.  Restoring B4+C4 at step 10 may not rescue step 9.
        for cutoff in (9, 10):
            with self.subTest(cutoff=cutoff):
                with self.assertRaisesRegex(ValueError, 'credit exceeded'):
                    reconcile(archive(events=self.amended(), as_known=cutoff))

    def test_amend_must_preserve_input_total_weight(self):
        events = timeline() + [
            amend('AM2', 'M0', [draw('R1', 'B', '4'), draw('R1', 'C', '5')])]
        with self.assertRaisesRegex(ValueError, 'total weight'):
            reconcile(archive(events=events, as_known=9))

    def test_amend_switching_a_submix_source_recomputes_share(self):
        data = {
            'receipts': [receipt('R1', [('O', 'a', 'A', '2'),
                                       ('O', 'b', 'B', '4')])],
            'orders': [{'id': 'O', 'demand_grams': '10'}],
            'events': [mix('M0', [draw('R1', 'A', '2')], '2'),
                       mix('M1', [sub('M0', '2'), draw('R1', 'B', '2')], '4'),
                       pack('P1', 'M1', '2'),
                       amend('AM', 'M1', [sub('M0', '1'),
                                         draw('R1', 'B', '3')])],
            'lot_ids': ['A'], 'compare_to': 3, 'as_known': 4}
        report = reconcile(data)
        # Before: M1's 4g input is half watched (2g A), so its 4g output
        # carries 2g and the 2g parcel carries 1g.  After: only 1g A of 4g
        # input survives, so the same parcel carries 1/2g.
        self.assertEqual(report['before']['parcels'][0]['attributed_grams'], '1')
        self.assertEqual(report['after']['parcels'][0]['attributed_grams'], '1/2')

    def test_amend_referencing_future_mix_rejected(self):
        events = timeline() + [amend('AM2', 'M0', [sub('M2', '3'),
                                                   draw('R1', 'C', '3')])]
        with self.assertRaisesRegex(ValueError, 'not earlier'):
            reconcile(archive(events=events, as_known=9))

    def test_receipt_credit_cannot_be_reused_downstream(self):
        events = copy.deepcopy(timeline())
        events[2] = mix('M2', [draw('R1', 'C', '2'), draw('R2', 'A', '1')], '2')
        with self.assertRaisesRegex(ValueError, 'credit exceeded'):
            reconcile(archive(events=events))

    def test_pack_and_mix_cannot_jointly_overdraw(self):
        events = copy.deepcopy(timeline())
        events[4] = pack('P2', 'M2', '2')  # M2 had 2g; P3 later wants 2g more
        with self.assertRaisesRegex(ValueError, 'overdraws mix M2'):
            reconcile(archive(events=events))

    def test_reference_errors_rejected(self):
        cases = []
        events = copy.deepcopy(timeline()); events[3] = pack('P1', 'M9', '1')
        cases.append((events, 'unknown mix'))
        events = copy.deepcopy(timeline()); events[6] = ship('S1', 'P9')
        cases.append((events, 'unknown parcel'))
        events = copy.deepcopy(timeline()); events.insert(7, ship('S2', 'P1'))
        cases.append((events, 'twice'))
        events = copy.deepcopy(timeline()); events.insert(0, pack('P0', 'M0', '1'))
        cases.append((events, 'registered later'))
        for events, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(ValueError, fragment):
                    reconcile(archive(events=events))


class AttributionTests(unittest.TestCase):
    def test_split_loss_and_nested_shares(self):
        data = {
            'receipts': [receipt('R1', [('O', 'x:0', 'A', '8'),
                                       ('O', 'x:1', 'B', '2')])],
            'orders': [{'id': 'O', 'demand_grams': '100'}],
            'events': [mix('M0', [draw('R1', 'A', '8'), draw('R1', 'B', '2')], '6'),
                       mix('M1', [sub('M0', '5')], '5'),
                       pack('P1', 'M1', '2')],
            'lot_ids': ['A'], 'compare_to': 3, 'as_known': 3}
        row = by_id(reconcile(data)['after']['parcels'])['P1']
        # 8/10 of the 10g M0 input is watched, so its 6g output carries 24/5.
        # M1 takes all 5g of it and passes the share through, then packing 2g
        # of the 5g output attributes 2 * (4/5) = 8/5.
        self.assertEqual(row['attributed_grams'], '8/5')

    def test_amendment_switch_to_zero_returns_credit(self):
        data = {
            'receipts': [receipt('R1', [('O', 'x:0', 'A', '4'),
                                       ('O', 'x:1', 'B', '4')])],
            'orders': [{'id': 'O', 'demand_grams': '10'}],
            'events': [mix('M0', [draw('R1', 'A', '4')], '4'),
                       pack('P0', 'M0', '1'),
                       amend('AM1', 'M0', [draw('R1', 'B', '4')])],
            'lot_ids': ['A'], 'compare_to': 2, 'as_known': 3}
        report = reconcile(data)
        self.assertEqual(by_id(report['before']['parcels'])['P0']['action'],
                         'hold')
        after = by_id(report['after']['parcels'])['P0']
        self.assertEqual(after['attributed_grams'], '0')
        self.assertEqual(after['action'], 'ready')


class ValidationTests(unittest.TestCase):
    def base(self):
        return archive()

    def assert_invalid(self, mutate, fragment):
        data = self.base()
        mutate(data)
        with self.assertRaisesRegex(ValueError, fragment):
            reconcile(data)

    def test_receipt_rules(self):
        def duplicate_leaf(data):
            data['receipts'][0]['lines'].append(
                copy.deepcopy(data['receipts'][0]['lines'][0]))
        self.assert_invalid(duplicate_leaf, 'duplicate receipt leaf')

        def duplicate_lot(data):
            line = data['receipts'][0]['lines'][0]
            line['draws'].append({'lot_id': 'A', 'gross': '1', 'cost': '0'})
            line['gross'] = '5'; line['net'] = '5'
        self.assert_invalid(duplicate_lot, 'duplicate lot')

        def non_positive(data):
            data['receipts'][0]['lines'][0]['net'] = '0'
        self.assert_invalid(non_positive, 'positive')

        def bad_sum(data):
            data['receipts'][0]['lines'][0]['draws'][0]['gross'] = '3'
        self.assert_invalid(bad_sum, 'draw gross total')

        def duplicate_request(data):
            data['receipts'].append(copy.deepcopy(data['receipts'][1]))
        self.assert_invalid(duplicate_request, 'duplicate identity')

    def test_watched_lot_must_be_drawn_and_bounded(self):
        def absent(data):
            data['lot_ids'] = ['Z']
        self.assert_invalid(absent, 'does not appear')
        for lot_ids in ([], ['A', 'A'], [f'L{i}' for i in range(31)]):
            self.assert_invalid(lambda d, v=lot_ids: d.update(lot_ids=v),
                                'array size|duplicate identity')

    def test_cutoff_bounds(self):
        for compare_to, as_known in ((8, 7), (-1, 0), (0, 9)):
            self.assert_invalid(
                lambda d, a=compare_to, b=as_known: d.update(
                    compare_to=a, as_known=b), 'integer 0..8|may not exceed|integer 0..')

    def test_unknown_fields_and_types(self):
        self.assert_invalid(lambda d: d.update(extra=1), 'expected fields')
        self.assert_invalid(lambda d: d['orders'][0].update(demand_grams=5),
                            'bounded decimal')
        self.assert_invalid(lambda d: d.update(compare_to='7'), 'integer')
        self.assert_invalid(lambda d: d['events'][0].update(kind='grind'),
                            'invalid event kind')
        self.assert_invalid(
            lambda d: d['events'][0]['inputs'][0].update(request_id='R9'),
            'receipt credit exceeded|absent from the archive')

    def test_output_is_excluded_beyond_as_known(self):
        # A garbage record after as_known cannot poison this report.
        data = self.base()
        data['events'].append({'id': 'JUNK', 'kind': 'unknown'})
        data['as_known'] = 8
        self.assertEqual(reconcile(data)['action_changes'],
                         [{'id': 'P2', 'before': 'hold', 'after': 'ready'}])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj))
        return str(path)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_fixture_end_to_end(self):
        source = str(ROOT / 'fixtures' / 'rework.json')
        output = str(self.dir / 'report.json')
        code, out, err = self.run_cli(
            ['rework-reconcile', '--source', source, '--output', output])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out),
                         {'changes': 1, 'orders': 1, 'parcels': 3})
        report = json.loads(Path(output).read_text())
        self.assertEqual(report['action_changes'],
                         [{'id': 'P2', 'before': 'hold', 'after': 'ready'}])
        # A second run must not overwrite the published report.
        before = Path(output).read_bytes()
        code, out, err = self.run_cli(
            ['rework-reconcile', '--source', source, '--output', output])
        self.assertEqual(code, 2)
        self.assertEqual(Path(output).read_bytes(), before)

    def test_overdraw_fixture_rejected_without_report(self):
        source = str(ROOT / 'fixtures' / 'rework-overdraw.json')
        output = str(self.dir / 'never.json')
        code, out, err = self.run_cli(
            ['rework-reconcile', '--source', source, '--output', output])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('credit exceeded', err)
        self.assertFalse(Path(output).exists())
        self.assertEqual(list(self.dir.glob('.publish-*')), [])

    def test_duplicate_json_key_rejected(self):
        raw = json.dumps(archive())[:-1] + ',"lot_ids":["A"]}'
        source = self.dir / 'dup.json'
        source.write_text(raw)
        output = str(self.dir / 'out.json')
        code, out, err = self.run_cli(
            ['rework-reconcile', '--source', str(source), '--output', output])
        self.assertEqual(code, 2)
        self.assertIn('duplicate JSON key', err)
        self.assertFalse(Path(output).exists())

    def test_module_entry_point_end_to_end(self):
        import os
        import subprocess
        source = str(ROOT / 'fixtures' / 'rework.json')
        output = str(self.dir / 'report.json')
        result = subprocess.run(
            [sys.executable, '-m', 'recipe_yield', 'rework-reconcile',
             '--source', source, '--output', output],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'changes': 1, 'orders': 1, 'parcels': 3})
        bad = subprocess.run(
            [sys.executable, '-m', 'recipe_yield', 'rework-reconcile',
             '--source', str(ROOT / 'fixtures' / 'rework-overdraw.json'),
             '--output', str(self.dir / 'nope.json')],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(bad.returncode, 2)
        self.assertFalse((self.dir / 'nope.json').exists())

    def test_legacy_commands_still_route(self):
        source = str(ROOT / 'fixtures' / 'input.json')
        output = str(self.dir / 'legacy.json')
        code, out, err = self.run_cli(['--source', source, '--output', output])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out),
                         {'cost': '16', 'ingredients': 2, 'orders': 1})
        code, out, err = self.run_cli(
            ['stock-show', '--ledger', str(self.dir / 'missing')])
        self.assertEqual(code, 2)


class SourceFixtureTests(unittest.TestCase):
    def test_every_committed_archive_parses(self):
        for path in sorted((ROOT / 'fixtures').glob('rework*.json')):
            data = json.loads(path.read_text())
            if path.name == 'rework-overdraw.json':
                self.assertRaises(ValueError, reconcile, data)
            else:
                reconcile(data)


if __name__ == '__main__':
    unittest.main()
