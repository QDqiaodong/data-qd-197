import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from recipe_yield import cli


def receipt(request_id, lines, ingredient='flour'):
    # lines: list of (order_id, path, lot_id, gross)
    return {'request_id': request_id, 'use_on': '2027-01-01',
            'total_cost': '0',
            'lines': [{'order_id': order_id, 'path': [path],
                       'ingredient': ingredient, 'net': gross,
                       'gross': gross, 'trim': '0', 'cost': '0',
                       'tags': [],
                       'draws': [{'lot_id': lot_id, 'gross': gross,
                                  'cost': '0'}]}
                      for order_id, path, lot_id, gross in lines]}


def close(period, previous, receipts, confirmed):
    return {'period': period, 'previous': previous,
            'receipts': receipts,
            'confirmed': [{'lot_id': lot_id, 'cents': cents}
                          for lot_id, cents in confirmed]}


class SettleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj))
        return str(path)

    def write_raw(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def proc_env(self, extra=None):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        if extra:
            env.update(extra)
        return env

    def run_proc(self, args, env_extra=None):
        return subprocess.run([sys.executable, '-m', 'recipe_yield'] + args,
                              capture_output=True,
                              env=self.proc_env(env_extra))

    def start_proc(self, args, env_extra=None):
        return subprocess.Popen([sys.executable, '-m', 'recipe_yield'] + args,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=self.proc_env(env_extra))

    def init_book(self, name, lots):
        book = str(self.dir / name)
        code, out, err = self.run_cli(
            ['settle-init', '--book', book,
             '--source', self.write(name + '.json', {'lots': lots})])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '')
        return book

    def settle(self, book, obj, name='close.json'):
        return self.run_cli(
            ['settle-close', '--book', book,
             '--source', self.write(name, obj)])

    def export(self, book, period, output):
        return self.run_cli(['settle-export', '--book', book,
                             '--period', period, '--output', output])

    def voucher(self, book, period, name=None):
        out_path = str(self.dir / (name or f'v-{period}.json'))
        code, out, err = self.export(book, period, out_path)
        self.assertEqual(code, 0, err)
        return json.loads(Path(out_path).read_text()), out_path

    def assert_rejected(self, code, out, err, *fragments):
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, '')
        for fragment in fragments:
            self.assertIn(fragment, err)

    @staticmethod
    def lot(lot_id='L', ingredient='flour', received='7'):
        return {'lot_id': lot_id, 'ingredient': ingredient,
                'received_grams': received}

    @staticmethod
    def by_bucket(voucher):
        return {(entry['lot_id'], entry['order_id']): entry['delta_cents']
                for entry in voucher['entries']}


class AllocationTests(SettleCase):
    def build_three_period_book(self):
        book = self.init_book('book', [self.lot()])
        r1 = receipt('R1', [('A', 'x:0', 'L', '2'), ('A', 'x:1', 'L', '1'),
                            ('B', 'y:0', 'L', '3')])
        r3 = receipt('R3', [('C', 'z:0', 'L', '1')])
        code, out, err = self.settle(
            book, close('p1', None, [r1], [('L', 3)]), 'c1.json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'period': 'p1', 'previous': None})
        code, _, err = self.settle(
            book, close('p2', 'p1', [], [('L', 4)]), 'c2.json')
        self.assertEqual(code, 0, err)
        code, _, err = self.settle(
            book, close('p3', 'p2', [r3], [('L', 3)]), 'c3.json')
        self.assertEqual(code, 0, err)
        return book

    def test_three_period_target_deltas_and_reversals(self):
        book = self.build_three_period_book()
        v1, _ = self.voucher(book, 'p1')
        self.assertEqual(self.by_bucket(v1),
                         {('L', 'A'): 1, ('L', 'B'): 1, ('L', None): 1})
        v2, _ = self.voucher(book, 'p2')
        self.assertEqual(self.by_bucket(v2),
                         {('L', 'A'): 1, ('L', 'B'): 1, ('L', None): -1})
        v3, _ = self.voucher(book, 'p3')
        # Stock bucket disappears once the full gross is drawn.
        self.assertEqual(self.by_bucket(v3),
                         {('L', 'A'): -1, ('L', 'B'): -1, ('L', 'C'): 1})
        # Cumulative per-order targets after three periods: each order 1.
        totals = {}
        for voucher in (v1, v2, v3):
            for entry in voucher['entries']:
                key = (entry['lot_id'], entry['order_id'])
                totals[key] = totals.get(key, 0) + entry['delta_cents']
        self.assertEqual(totals, {('L', 'A'): 1, ('L', 'B'): 1, ('L', 'C'): 1,
                                  ('L', None): 0})
        # Entry ordering: lot id, then order id ASCII, null last within a lot.
        keys = [(e['lot_id'], e['order_id']) for e in v3['entries']]
        self.assertEqual(keys, [('L', 'A'), ('L', 'B'), ('L', 'C')])

    def test_exact_tie_orders_win_before_unclaimed_stock(self):
        book = self.init_book('tie', [self.lot(received='1')])
        r = receipt('R1', [('a', 'x:0', 'L', '1/3'),
                           ('b', 'x:1', 'L', '1/3')])
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L', 2)]))
        self.assertEqual(code, 0, err)
        v, _ = self.voucher(book, 'p1')
        self.assertEqual(self.by_bucket(v), {('L', 'a'): 1, ('L', 'b'): 1})
        self.assertNotIn(('L', None), self.by_bucket(v))

    def test_tie_break_is_ascii_order_id(self):
        book = self.init_book('ascii', [self.lot(received='3')])
        r = receipt('R1', [('B', 'x:0', 'L', '1'), ('A', 'x:1', 'L', '1')])
        # Weights 1/3 each; stock also 1/3. One cent: all floors zero, the
        # single remainder cent follows ASCII order among order buckets.
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L', 1)]))
        self.assertEqual(code, 0, err)
        v, _ = self.voucher(book, 'p1')
        self.assertEqual(self.by_bucket(v), {('L', 'A'): 1})

    def test_limited_extra_cents_follow_ascii_orders_before_stock(self):
        # Four equal-weight buckets: orders c, a, b and the stock bucket.
        # Two extra cents with all floors zero go to orders a and b only;
        # c and stock get nothing, proving both rules at once.
        book = self.init_book('four', [self.lot(received='4')])
        r = receipt('R1', [('c', 'x:0', 'L', '1'),
                           ('a', 'x:1', 'L', '1'),
                           ('b', 'x:2', 'L', '1')])
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L', 2)]))
        self.assertEqual(code, 0, err)
        v, _ = self.voucher(book, 'p1')
        self.assertEqual(self.by_bucket(v),
                         {('L', 'a'): 1, ('L', 'b'): 1})
        self.assertNotIn(('L', 'c'), self.by_bucket(v))
        self.assertNotIn(('L', None), self.by_bucket(v))

    def test_payment_cut_can_reverse_every_bucket(self):
        book = self.init_book('cut', [self.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3'), ('B', 'y:0', 'L', '3')])
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L', 7)]))
        self.assertEqual(code, 0, err)
        v1, _ = self.voucher(book, 'p1')
        # 3/7 and 3/7 of 7 are exact (3,3); stock gets 1.
        self.assertEqual(self.by_bucket(v1),
                         {('L', 'A'): 3, ('L', 'B'): 3, ('L', None): 1})
        code, _, err = self.settle(
            book, close('p2', 'p1', [], [('L', 0)]))
        self.assertEqual(code, 0, err)
        v2, _ = self.voucher(book, 'p2')
        self.assertEqual(self.by_bucket(v2),
                         {('L', 'A'): -3, ('L', 'B'): -3, ('L', None): -1})
        # Negative deltas are never clamped to zero.
        self.assertTrue(all(e['delta_cents'] < 0 for e in v2['entries']))

    def test_lots_are_allocated_independently(self):
        book = self.init_book(
            'multi', [self.lot('L1', received='2'), self.lot('L2', received='2')])
        r = receipt('R1', [('A', 'x:0', 'L1', '1'),
                           ('A', 'x:1', 'L2', '1')])
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L1', 3), ('L2', 1)]))
        self.assertEqual(code, 0, err)
        v, _ = self.voucher(book, 'p1')
        # L1: order 1/2 -> floor 1 rem .5, stock 1/2 -> floor 1 rem .5;
        # order wins the tie. L2: 1/2 and 1/2 of 1 -> order gets the cent.
        self.assertEqual(self.by_bucket(v),
                         {('L1', 'A'): 2, ('L1', None): 1, ('L2', 'A'): 1})
        keys = [(e['lot_id'], e['order_id']) for e in v['entries']]
        self.assertEqual(keys, [('L1', 'A'), ('L1', None), ('L2', 'A')])

    def test_zero_delta_period_still_closes_and_exports_empty_entries(self):
        book = self.init_book('zero', [self.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3')])
        code, _, err = self.settle(
            book, close('p1', None, [r], [('L', 0)]))
        self.assertEqual(code, 0, err)
        v1, path1 = self.voucher(book, 'p1')
        self.assertEqual(v1, {'period': 'p1', 'previous': None, 'entries': []})
        code, _, err = self.settle(
            book, close('p2', 'p1', [], [('L', 0)]))
        self.assertEqual(code, 0, err)
        v2, _ = self.voucher(book, 'p2')
        self.assertEqual(v2['entries'], [])
        # The archive and chain advanced even with nothing to post.
        code, _, err = self.settle(
            book, close('p3', 'p2', [], [('L', 0)]))
        self.assertEqual(code, 0, err)


class RealReceiptTests(SettleCase):
    def stock_lots(self):
        return {'lots': [{'lot_id': 'L', 'ingredient': 'flour',
                          'expires_on': '2027-12-31', 'gross_grams': '7',
                          'price_per_gross_gram': '0', 'tags': []}]}

    def issue_request(self, request_id, order_id, recipe, leaves):
        components = [{'kind': 'ingredient', 'ref': 'flour',
                       'net_grams': weight} for weight in leaves]
        return {'ingredients': [{'id': 'flour', 'yield': '1',
                                 'price_per_gross_gram': '0', 'tags': []}],
                'recipes': [{'id': recipe,
                             'output_grams': str(sum(float(w) for w in leaves)),
                             'components': components}],
                'orders': [{'id': order_id, 'recipe': recipe,
                            'output_grams': str(sum(float(w) for w in leaves))}],
                'request_id': request_id, 'use_on': '2027-01-01',
                'restrictions': []}

    def test_original_stock_issue_receipts_flow_into_settlement(self):
        stock = str(self.dir / 'stock')
        code, _, err = self.run_cli(
            ['stock-init', '--ledger', stock,
             '--source', self.write('stocklots.json', self.stock_lots())])
        self.assertEqual(code, 0, err)
        # Order A's 3 grams are split across two leaves of the same receipt.
        code, out, err = self.run_cli(
            ['stock-issue', '--ledger', stock,
             '--source', self.write('issueA.json',
                                    self.issue_request('R-A', 'A', 'ra',
                                                       ['1.5', '1.5']))])
        self.assertEqual(code, 0, err)
        receipt_a = json.loads(out)
        self.assertEqual(len(receipt_a['lines']), 2)
        code, out, err = self.run_cli(
            ['stock-issue', '--ledger', stock,
             '--source', self.write('issueB.json',
                                    self.issue_request('R-B', 'B', 'rb',
                                                       ['3']))])
        self.assertEqual(code, 0, err)
        receipt_b = json.loads(out)

        book = self.init_book(
            'settle', [{'lot_id': 'L', 'ingredient': 'flour',
                        'received_grams': '7'}])
        code, out, err = self.settle(
            book, close('p1', None, [receipt_a, receipt_b], [('L', 3)]))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'period': 'p1', 'previous': None})
        v, _ = self.voucher(book, 'p1')
        self.assertEqual(self.by_bucket(v),
                         {('L', 'A'): 1, ('L', 'B'): 1, ('L', None): 1})


class RetryAndChainTests(SettleCase):
    def setUp(self):
        super().setUp()
        self.book = self.init_book('book', [SettleCase.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3')])
        self.c1 = close('p1', None, [r], [('L', 3)])
        code, self.first_out, err = self.settle(self.book, self.c1)
        self.assertEqual(code, 0, err)

    def test_identical_retry_returns_original_result(self):
        path = self.write('retry.json', self.c1)
        code, out, err = self.run_cli(
            ['settle-close', '--book', self.book, '--source', path])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.first_out)

    def test_retry_ignores_whitespace_and_key_order_but_keeps_values(self):
        reordered = {'confirmed': self.c1['confirmed'],
                     'receipts': self.c1['receipts'],
                     'previous': self.c1['previous'],
                     'period': self.c1['period']}
        path = self.write_raw('reorder.json', json.dumps(reordered, indent=4))
        code, out, err = self.run_cli(
            ['settle-close', '--book', self.book, '--source', path])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.first_out)

    def test_same_period_different_content_rejected(self):
        changed = close('p1', None, [], [('L', 4)])
        code, out, err = self.settle(self.book, changed, 'changed.json')
        self.assert_rejected(code, out, err, 'already closed')

    def test_retry_does_not_require_previous_to_stay_last(self):
        code, _, err = self.settle(
            self.book, close('p2', 'p1', [], [('L', 4)]), 'c2.json')
        self.assertEqual(code, 0, err)
        # p1 retried even though p2 is the current last period now.
        code, out, err = self.run_cli(
            ['settle-close', '--book', self.book,
             '--source', self.write('retry.json', self.c1)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'period': 'p1', 'previous': None})

    def test_only_current_last_period_may_be_extended(self):
        code, _, err = self.settle(
            self.book, close('p2', 'p1', [], [('L', 4)]), 'c2.json')
        self.assertEqual(code, 0, err)
        for bad_previous in (None, 'p0'):
            code, out, err = self.settle(
                self.book, close('p3', bad_previous, [], [('L', 4)]),
                f'bad-{bad_previous}.json')
            self.assert_rejected(code, out, err, 'previous')
        # Period ids carry no ordering semantics: a "smaller" id is fine as
        # long as it extends the current head.
        code, _, err = self.settle(
            self.book, close('p0', 'p2', [], [('L', 4)]), 'p0.json')
        self.assertEqual(code, 0, err)

    def test_first_period_requires_null_previous(self):
        book = self.init_book('fresh', [SettleCase.lot()])
        code, out, err = self.settle(
            book, close('p1', 'ghost', [], [('L', 0)]))
        self.assert_rejected(code, out, err, 'previous must be null')

    def test_period_id_unique_even_with_matching_previous(self):
        code, out, err = self.settle(
            self.book, close('p1', 'p1', [], [('L', 4)]))
        self.assert_rejected(code, out, err, 'already closed')


class ArchiveIdentityTests(SettleCase):
    def setUp(self):
        super().setUp()
        self.book = self.init_book('book', [
            SettleCase.lot('L1', 'flour', '7'),
            SettleCase.lot('L2', 'sugar', '7')])
        self.good = receipt('R1', [('A', 'x:0', 'L1', '3')])

    def reject(self, obj, fragment):
        code, out, err = self.settle(self.book, obj, 'bad.json')
        self.assert_rejected(code, out, err, fragment)

    def test_duplicate_leaf_identity_rejected(self):
        bad = receipt('R1', [('A', 'x:0', 'L1', '1'),
                             ('A', 'x:0', 'L1', '2')])
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'duplicate receipt leaf identity')

    def test_duplicate_lot_within_leaf_rejected(self):
        bad = {'request_id': 'R1', 'use_on': '2027-01-01', 'total_cost': '0',
               'lines': [{'order_id': 'A', 'path': ['x:0'],
                          'ingredient': 'flour', 'net': '2', 'gross': '2',
                          'trim': '0', 'cost': '0', 'tags': [],
                          'draws': [{'lot_id': 'L1', 'gross': '1', 'cost': '0'},
                                    {'lot_id': 'L1', 'gross': '1',
                                     'cost': '0'}]}]}
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'duplicate lot')

    def test_non_positive_draw_rejected(self):
        bad = receipt('R1', [('A', 'x:0', 'L1', '0')])
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'must be positive')

    def test_leaf_gross_must_equal_draw_sum(self):
        bad = {'request_id': 'R1', 'use_on': '2027-01-01', 'total_cost': '0',
               'lines': [{'order_id': 'A', 'path': ['x:0'],
                          'ingredient': 'flour', 'net': '3', 'gross': '3',
                          'trim': '0', 'cost': '0', 'tags': [],
                          'draws': [{'lot_id': 'L1', 'gross': '2',
                                     'cost': '0'}]}]}
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'draw gross total')

    def test_unknown_lot_rejected(self):
        bad = receipt('R1', [('A', 'x:0', 'LX', '3')])
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'unknown lot')

    def test_ingredient_mismatch_rejected(self):
        # Leaf claims flour but the draw points at a sugar lot.
        bad = receipt('R1', [('A', 'x:0', 'L2', '3')], ingredient='flour')
        self.reject(close('p1', None, [bad], [('L1', 0), ('L2', 0)]),
                    'ingredient does not match')

    def test_request_id_reported_again_in_later_period_rejected(self):
        code, _, err = self.settle(
            self.book, close('p1', None, [self.good], [('L1', 0), ('L2', 0)]))
        self.assertEqual(code, 0, err)
        code, out, err = self.settle(
            self.book, close('p2', 'p1', [self.good], [('L1', 0), ('L2', 0)]))
        self.assert_rejected(code, out, err, 'already archived')

    def test_duplicate_request_id_within_one_request_rejected(self):
        other = receipt('R1', [('A', 'x:1', 'L1', '1')])
        code, out, err = self.settle(
            self.book, close('p1', None, [self.good, other],
                             [('L1', 0), ('L2', 0)]))
        self.assert_rejected(code, out, err, 'duplicate request_id')

    def test_same_leaf_identity_across_receipts_merges_by_order(self):
        # Leaf identity uniqueness is per receipt; the same leaf on two
        # receipts merges into one order bucket during aggregation.
        first = receipt('R1', [('A', 'x:0', 'L1', '3')])
        second = receipt('R2', [('A', 'x:0', 'L1', '2')])
        code, _, err = self.settle(
            self.book, close('p1', None, [first, second],
                             [('L1', 7), ('L2', 0)]))
        self.assertEqual(code, 0, err)
        voucher, _ = self.voucher(self.book, 'p1')
        # L1: order A holds 5/7 of the gross, stock the remaining 2/7.
        # 7 cents splits exactly 5 to A, 2 to stock.
        self.assertEqual(self.by_bucket(voucher),
                         {('L1', 'A'): 5, ('L1', None): 2})

    def test_rejections_do_not_move_chain_position(self):
        code, _, _ = self.settle(
            self.book, close('p1', None, [self.good], [('L1', 0), ('L2', 0)]))
        self.assertEqual(code, 0)
        over = receipt('R2', [('A', 'x:9', 'L1', '5')])  # 3 + 5 > 7
        code, out, err = self.settle(
            self.book, close('p2', 'p1', [over], [('L1', 0), ('L2', 0)]))
        self.assert_rejected(code, out, err, 'exceed received gross')
        # p2 is still open: a fresh period with the same id succeeds.
        code, _, err = self.settle(
            self.book, close('p2', 'p1', [], [('L1', 1), ('L2', 0)]),
            'recover.json')
        self.assertEqual(code, 0, err)
        # The rejected receipt's request id was never occupied.
        code, _, err = self.settle(
            self.book, close('p3', 'p2',
                             [receipt('R2', [('B', 'y:0', 'L1', '1')])],
                             [('L1', 1), ('L2', 0)]))
        self.assertEqual(code, 0, err)


class StrictValidationTests(SettleCase):
    def test_init_refuses_existing_path_and_bad_lots(self):
        book = self.init_book('book', [SettleCase.lot()])
        code, out, err = self.run_cli(
            ['settle-init', '--book', book,
             '--source', self.write('x.json', {'lots': [SettleCase.lot()]})])
        self.assert_rejected(code, out, err, 'already exists')
        for bad in (
                {'lots': []},
                {'lots': [{'lot_id': 'A', 'ingredient': 'flour',
                           'received_grams': '0'}]},
                {'lots': [{'lot_id': 'A', 'ingredient': 'flour',
                           'received_grams': '-1'}]},
                {'lots': [{'lot_id': 'A', 'ingredient': 'flour',
                           'received_grams': 3}]},
                {'lots': [{'lot_id': 'A', 'ingredient': 'flour',
                           'received_grams': '1', 'extra': 1}]},
                {'lots': [SettleCase.lot('A'), SettleCase.lot('A')]}):
            code, out, err = self.run_cli(
                ['settle-init', '--book', str(self.dir / 'nope'),
                 '--source', self.write('bad.json', bad)])
            self.assert_rejected(code, out, err, 'error')

    def test_duplicate_json_keys_rejected(self):
        path = self.write_raw('dup.json', '{"lots": [], "lots": []}')
        code, out, err = self.run_cli(
            ['settle-init', '--book', str(self.dir / 'b'), '--source', path])
        self.assert_rejected(code, out, err, 'duplicate JSON key')

    def test_confirmed_must_cover_every_lot_once_with_integer_cents(self):
        book = self.init_book('book', [SettleCase.lot('L1'),
                                       SettleCase.lot('L2')])
        cases = [
            # Missing a lot, an unknown lot, a duplicate, out-of-range cents.
            close('p1', None, [], [('L1', 0)]),
            close('p1', None, [], [('L1', 0), ('L1', 1)]),
            close('p1', None, [], [('L1', 0), ('LX', 0)]),
            close('p1', None, [], [('L1', 0), ('L2', -1)]),
            close('p1', None, [], [('L1', 0), ('L2', 10 ** 12 + 1)]),
        ]
        for obj in cases:
            code, out, err = self.settle(book, obj)
            self.assert_rejected(code, out, err, 'error')
        # Float cents are a type error, never silently rounded.
        obj = close('p1', None, [], [('L1', 0), ('L2', 1.5)])
        code, out, err = self.settle(book, obj)
        self.assert_rejected(code, out, err, 'error')

    def test_unknown_top_level_fields_rejected(self):
        book = self.init_book('book', [SettleCase.lot()])
        obj = close('p1', None, [], [('L', 0)])
        obj['extra'] = True
        self.assert_rejected(*self.settle(book, obj), 'error')

    def test_receipt_count_bounded(self):
        book = self.init_book('book', [SettleCase.lot(received='1000')])
        receipts = [receipt(f'R{i}', [(f'O{i}', f'x:0', 'L', '1')])
                    for i in range(101)]
        code, out, err = self.settle(
            book, close('p1', None, receipts, [('L', 0)]))
        self.assert_rejected(code, out, err, 'array size')

    def test_missing_book_and_unclosed_period(self):
        code, out, err = self.run_cli(
            ['settle-close', '--book', str(self.dir / 'absent'),
             '--source', self.write('c.json', close('p1', None, [], []))])
        self.assert_rejected(code, out, err, 'book not found')
        book = self.init_book('book', [SettleCase.lot()])
        code, out, err = self.export(book, 'ghost', str(self.dir / 'v.json'))
        self.assert_rejected(code, out, err, 'not closed')


class ExportTests(SettleCase):
    def setUp(self):
        super().setUp()
        self.book = self.init_book('book', [SettleCase.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3'), ('B', 'y:0', 'L', '3')])
        code, _, err = self.settle(
            self.book, close('p1', None, [r], [('L', 3)]))
        self.assertEqual(code, 0, err)

    def test_repeat_exports_are_byte_identical(self):
        _, first = self.voucher(self.book, 'p1', 'a.json')
        _, second = self.voucher(self.book, 'p1', 'b.json')
        self.assertEqual(Path(first).read_bytes(), Path(second).read_bytes())

    def test_existing_identical_file_is_success_other_content_rejected(self):
        _, path = self.voucher(self.book, 'p1', 'same.json')
        code, _, err = self.export(self.book, 'p1', path)
        self.assertEqual(code, 0, err)
        different = self.dir / 'different.json'
        different.write_text('{"nope": true}\n')
        code, out, err = self.export(self.book, 'p1', str(different))
        self.assert_rejected(code, out, err, 'differs from sealed voucher')
        self.assertEqual(different.read_text(), '{"nope": true}\n')

    def test_directories_and_symlinks_are_rejected_even_with_same_bytes(self):
        _, path = self.voucher(self.book, 'p1', 'real.json')
        directory = self.dir / 'folder'
        directory.mkdir()
        code, out, err = self.export(self.book, 'p1', str(directory))
        self.assert_rejected(code, out, err, 'not a regular file')
        link = self.dir / 'link.json'
        link.symlink_to(path)
        code, out, err = self.export(self.book, 'p1', str(link))
        self.assert_rejected(code, out, err, 'symlink output rejected')
        # Rejections never block delivery to a fresh empty path.
        fresh = self.dir / 'fresh.json'
        code, _, err = self.export(self.book, 'p1', str(fresh))
        self.assertEqual(code, 0, err)
        self.assertEqual(fresh.read_bytes(), Path(path).read_bytes())

    def test_next_period_closes_even_when_previous_not_delivered(self):
        # Finance delivery order is reconstructed from voucher.previous;
        # exporting p1 is deliberately skipped.
        r2 = receipt('R2', [('C', 'z:0', 'L', '1')])
        code, _, err = self.settle(
            self.book, close('p2', 'p1', [r2], [('L', 4)]), 'c2.json')
        self.assertEqual(code, 0, err)
        voucher, path = self.voucher(self.book, 'p2', 'p2.json')
        self.assertEqual(voucher['previous'], 'p1')
        self.assertFalse((self.dir / 'p1.json').exists())

    def test_export_needs_no_source_inputs(self):        # Moving the original close input away must not seal-break export.
        _, path = self.voucher(self.book, 'p1', 'kept.json')
        for source in self.dir.glob('*.json'):
            if source.name not in {Path(path).name}:
                source.rename(self.dir / f'archived-{source.name}')
        code, _, err = self.export(
            self.book, 'p1', str(self.dir / 'again.json'))
        self.assertEqual(code, 0, err)


class CrashTests(SettleCase):
    def build_chain(self, book):
        r1 = receipt('R1', [('A', 'x:0', 'L', '3'), ('B', 'y:0', 'L', '3')])
        result = self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('c1.json',
                                    close('p1', None, [r1], [('L', 3)]))])
        self.assertEqual(result.returncode, 0, result.stderr)
        r3 = receipt('R3', [('C', 'z:0', 'L', '1')])
        result = self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('c2.json',
                                    close('p2', 'p1', [], [('L', 4)]))])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.c3 = close('p3', 'p2', [r3], [('L', 3)])
        self.c3_path = self.write('c3.json', self.c3)

    def test_close_before_commit_leaves_no_occupied_period_or_receipts(self):
        book = self.init_book('book', [SettleCase.lot()])
        self.build_chain(book)
        result = self.run_proc(
            ['settle-close', '--book', book, '--source', self.c3_path],
            {'RECIPE_YIELD_CRASHPOINT': 'settle_before_commit'})
        self.assertEqual(result.returncode, 99)
        self.assertEqual(result.stdout, b'')
        # The period cannot be exported and the chain head is still p2.
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', str(self.dir / 'ghost.json')])
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.dir / 'ghost.json').exists())
        # Receipt R3 was not occupied: resubmitting the same period works.
        result = self.run_proc(
            ['settle-close', '--book', book, '--source', self.c3_path])
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', str(self.dir / 'v3.json')])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_close_after_commit_recovers_and_never_double_archives(self):
        book = self.init_book('book', [SettleCase.lot()])
        self.build_chain(book)
        result = self.run_proc(
            ['settle-close', '--book', book, '--source', self.c3_path],
            {'RECIPE_YIELD_CRASHPOINT': 'settle_after_commit'})
        self.assertEqual(result.returncode, 99)
        self.assertEqual(result.stdout, b'')
        # stdout never arrived, but the next period can already be closed.
        c4 = close('p4', 'p3', [], [('L', 2)])
        result = self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('c4.json', c4)])
        self.assertEqual(result.returncode, 0, result.stderr)
        # Retrying p3 returns the original result and consumes nothing again.
        result = self.run_proc(
            ['settle-close', '--book', book, '--source', self.c3_path])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'period': 'p3', 'previous': 'p2'})
        # Sealed voucher content matches the p3 deltas, not later cumulative.
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', str(self.dir / 'v3.json')])
        self.assertEqual(result.returncode, 0, result.stderr)
        voucher = json.loads((self.dir / 'v3.json').read_text())
        self.assertEqual(SettleCase.by_bucket(voucher),
                         {('L', 'A'): -1, ('L', 'B'): -1, ('L', 'C'): 1})
        # Same id with a changed amount must still be rejected after the fact.
        changed = dict(self.c3)
        changed['confirmed'] = [{'lot_id': 'L', 'cents': 9}]
        result = self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('c3-changed.json', changed)])
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'already closed', result.stderr)

    def test_export_after_file_appeared_retries_idempotently(self):
        book = self.init_book('book', [SettleCase.lot()])
        self.build_chain(book)
        self.assertEqual(self.run_proc(
            ['settle-close', '--book', book, '--source', self.c3_path]
        ).returncode, 0)
        target = str(self.dir / 'v3.json')
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', target],
            {'RECIPE_YIELD_CRASHPOINT': 'settle_export_after_publish'})
        self.assertEqual(result.returncode, 99)
        self.assertTrue(Path(target).is_file())
        before = Path(target).read_bytes()
        # Retry at the same path succeeds and leaves the bytes untouched.
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', target])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(target).read_bytes(), before)
        # Another path receives identical bytes.
        other = str(self.dir / 'v3-again.json')
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', other])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(other).read_bytes(), before)
        # A symlink pointing at the correct voucher is still refused, and a
        # fresh path still works afterwards.
        link = str(self.dir / 'link.json')
        os.symlink(target, link)
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', link])
        self.assertEqual(result.returncode, 2)
        fresh = str(self.dir / 'fresh.json')
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', 'p3',
             '--output', fresh])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(fresh).read_bytes(), before)


class ConcurrencyTests(SettleCase):
    def test_two_new_periods_contending_for_same_previous_one_wins(self):
        book = self.init_book('book', [SettleCase.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3')])
        self.assertEqual(self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('c1.json',
                                    close('p1', None, [r], [('L', 1)]))]
        ).returncode, 0)
        p1 = self.start_proc(
            ['settle-close', '--book', book,
             '--source', self.write('q1.json',
                                    close('q1', 'p1', [], [('L', 2)]))])
        p2 = self.start_proc(
            ['settle-close', '--book', book,
             '--source', self.write('q2.json',
                                    close('q2', 'p1', [], [('L', 2)]))])
        out1, err1 = p1.communicate()
        out2, err2 = p2.communicate()
        self.assertEqual(sorted([p1.returncode, p2.returncode]), [0, 2],
                         (err1, err2))
        results = [json.loads(out) for out in (out1, out2) if out]
        self.assertEqual(results, [{'period': 'q1', 'previous': 'p1'}]
                         if p1.returncode == 0 else
                         [{'period': 'q2', 'previous': 'p1'}])
        # Exactly one period landed and the loser cannot be exported.
        winner = 'q1' if p1.returncode == 0 else 'q2'
        loser = 'q2' if winner == 'q1' else 'q1'
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', loser,
             '--output', str(self.dir / 'loser.json')])
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.dir / 'loser.json').exists())
        result = self.run_proc(
            ['settle-export', '--book', book, '--period', winner,
             '--output', str(self.dir / 'winner.json')])
        self.assertEqual(result.returncode, 0, result.stderr)
        # The chain stays linear: the winner extends p1 and is the new head.
        result = self.run_proc(
            ['settle-close', '--book', book,
             '--source', self.write('after.json',
                                    close('p3', winner, [], [('L', 2)]))])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_identical_concurrent_retries_both_succeed_once(self):
        book = self.init_book('book', [SettleCase.lot()])
        r = receipt('R1', [('A', 'x:0', 'L', '3')])
        path = self.write('c1.json', close('p1', None, [r], [('L', 1)]))
        p1 = self.start_proc(['settle-close', '--book', book,
                              '--source', path])
        p2 = self.start_proc(['settle-close', '--book', book,
                              '--source', path])
        out1, err1 = p1.communicate()
        out2, err2 = p2.communicate()
        self.assertEqual((p1.returncode, p2.returncode), (0, 0),
                         (err1, err2))
        self.assertEqual(out1, out2)


class LegacyCompatibilityTests(SettleCase):
    def test_original_commands_remain_on_their_contracts(self):
        # Legacy read-only accounting still works and ignores the new book.
        report = str(self.dir / 'report.json')
        code, out, err = self.run_cli(
            ['--source', str(ROOT / 'fixtures' / 'input.json'),
             '--output', report])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out),
                         {'cost': '16', 'ingredients': 2, 'orders': 1})
        # stock ledger lifecycle is untouched.
        stock = str(self.dir / 'stock')
        code, _, err = self.run_cli(
            ['stock-init', '--ledger', stock,
             '--source', str(ROOT / 'fixtures' / 'lots.json')])
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            ['stock-issue', '--ledger', stock,
             '--source', str(ROOT / 'fixtures' / 'issue.json')])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['total_cost'], '40')
        code, out, err = self.run_cli(['stock-show', '--ledger', stock])
        self.assertEqual(code, 0, err)
        balances = {row['lot_id']: row['gross_remaining']
                    for row in json.loads(out)['lots']}
        self.assertEqual(balances, {'A': '0', 'B': '0', 'C': '8'})
        # label-preflight and rework-reconcile still publish.
        code, _, err = self.run_cli(
            ['label-preflight', '--source',
             str(ROOT / 'fixtures' / 'preflight.json'),
             '--output', str(self.dir / 'preflight.json')])
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli(
            ['rework-reconcile', '--source',
             str(ROOT / 'fixtures' / 'rework.json'),
             '--output', str(self.dir / 'rework.json')])
        self.assertEqual(code, 0, err)


if __name__ == '__main__':
    unittest.main()
