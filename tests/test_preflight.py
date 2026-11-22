import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from recipe_yield import cli
from recipe_yield.preflight import run, parse_preflight


def catalog(declarations, jobs, thresholds=None):
    return {
        'ingredients': [
            {'id': 'base', 'yield': '1', 'price_per_gross_gram': '1', 'tags': ['local']},
            {'id': 'plain', 'yield': '1', 'price_per_gross_gram': '1', 'tags': []},
        ],
        'recipes': [
            {'id': 'mix', 'output_grams': '4', 'components': [
                {'kind': 'ingredient', 'ref': 'base', 'net_grams': '3'},
                {'kind': 'ingredient', 'ref': 'plain', 'net_grams': '1'}]},
            {'id': 'tray', 'output_grams': '8', 'components': [
                {'kind': 'recipe', 'ref': 'mix', 'net_grams': '4'},
                {'kind': 'recipe', 'ref': 'mix', 'net_grams': '4'}]},
        ],
        'orders': [
            {'id': 'A', 'recipe': 'tray', 'output_grams': '8'},
            {'id': 'B', 'recipe': 'tray', 'output_grams': '8'},
        ],
        'thresholds': thresholds if thresholds is not None else [
            {'tag': 'local', 'min_share': '0.75'}],
        'declarations': declarations,
        'print_jobs': jobs,
    }


def job(plate, job_id='P1', order_ids=('A', 'B')):
    return {'id': job_id, 'order_ids': list(order_ids), 'tags': list(plate)}


class PreflightCase(unittest.TestCase):
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

    def assert_rejected(self, code, out, err, *fragments):
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, '')
        for fragment in fragments:
            self.assertIn(fragment, err)


class ScenarioOneTests(PreflightCase):
    def decl_suppress_restore(self):
        return [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'},
            {'order_id': 'A', 'path': ['tray:0', 'mix:0'], 'tag': 'local', 'action': 'restore'},
        ]

    def test_suppress_then_restore_approves_shared_plate(self):
        report = run(catalog(self.decl_suppress_restore(), [job(['local'])]))['report']
        a, b = report['orders']
        for order in (a, b):
            self.assertEqual(order['total_net'], '8')
            self.assertEqual(order['required_tags'], ['local'])
            evidence = order['evidence'][0]
            self.assertEqual(evidence['tag'], 'local')
            self.assertEqual(evidence['net'], '6')
            self.assertEqual(evidence['occurrences'], '2')
            self.assertEqual(evidence['first_path'], ['tray:0', 'mix:0'])
        self.assertEqual(report['print_jobs'], [
            {'id': 'P1', 'approved': True, 'orders': [
                {'order_id': 'A', 'missing': [], 'extra': []},
                {'order_id': 'B', 'missing': [], 'extra': []}]}])

    def test_suppress_without_restore_lowers_share_and_flags_extra(self):
        declarations = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'}]
        report = run(catalog(declarations, [job(['local'])]))['report']
        a, b = report['orders']
        self.assertEqual(a['required_tags'], [])
        self.assertEqual(a['evidence'][0],
                         {'tag': 'local', 'net': '3', 'occurrences': '1',
                          'first_path': ['tray:1', 'mix:0']})
        # B is completely unaffected by A's declarations.
        self.assertEqual(b['required_tags'], ['local'])
        self.assertEqual(b['evidence'][0]['net'], '6')
        self.assertEqual(b['evidence'][0]['occurrences'], '2')
        self.assertEqual(report['print_jobs'][0]['approved'], False)
        self.assertEqual(report['print_jobs'][0]['orders'], [
            {'order_id': 'A', 'missing': [], 'extra': ['local']},
            {'order_id': 'B', 'missing': [], 'extra': []}])

    def test_empty_plate_reports_missing_on_b(self):
        declarations = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'}]
        report = run(catalog(declarations, [job([])]))['report']
        self.assertFalse(report['print_jobs'][0]['approved'])
        self.assertEqual(report['print_jobs'][0]['orders'], [
            {'order_id': 'A', 'missing': [], 'extra': []},
            {'order_id': 'B', 'missing': ['local'], 'extra': []}])

    def test_bad_path_is_rejected_not_ignored(self):
        for bad_path, fragment in (
                (['tray:9'], 'index out of range'),
                (['tray:0', 'mix:9'], 'index out of range'),
                (['mix:0'], 'does not match'),
                (['tray:0', 'tray:0'], 'does not match'),
                (['tray:0', 'mix:0', 'base:0'], 'continues past an ingredient')):
            declarations = [{'order_id': 'A', 'path': bad_path,
                             'tag': 'local', 'action': 'suppress'}]
            with self.subTest(bad_path=bad_path):
                with self.assertRaisesRegex(ValueError, fragment):
                    run(catalog(declarations, [job(['local'])]))


class ScenarioTwoTests(PreflightCase):
    def chain_input(self):
        recipes = [{'id': 'r0', 'output_grams': '1', 'components': [
            {'kind': 'ingredient', 'ref': 'base', 'net_grams': '1'}]}]
        for k in range(1, 45):
            recipes.append({'id': f'r{k}', 'output_grams': '2', 'components': [
                {'kind': 'recipe', 'ref': f'r{k - 1}', 'net_grams': '1'},
                {'kind': 'recipe', 'ref': f'r{k - 1}', 'net_grams': '1'}]})
        return {
            'ingredients': [
                {'id': 'base', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': ['local']}],
            'recipes': recipes,
            'orders': [{'id': 'o', 'recipe': 'r44', 'output_grams': '2'}],
            'thresholds': [{'tag': 'local', 'min_share': '0.5'}],
            'declarations': [{'order_id': 'o', 'path': ['r44:0'],
                              'tag': 'local', 'action': 'suppress'}],
            'print_jobs': [job(['local'], order_ids=('o',))],
        }

    def test_exponential_chain_within_15_seconds(self):
        source = self.write('big.json', self.chain_input())
        output = str(self.dir / 'report.json')
        started = time.perf_counter()
        code, out, err = self.run_cli(['label-preflight', '--source', source,
                                       '--output', output])
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 15.0)
        report = json.loads(Path(output).read_text())
        order = report['orders'][0]
        self.assertEqual(order['total_net'], '2')
        evidence = order['evidence'][0]
        self.assertEqual(evidence['net'], '1')
        self.assertEqual(evidence['occurrences'], '8796093022208')
        expected_first = ['r44:1'] + [f'r{k}:0' for k in range(43, -1, -1)]
        self.assertEqual(evidence['first_path'], expected_first)
        self.assertEqual(len(evidence['first_path']), 45)
        self.assertEqual(order['required_tags'], ['local'])
        self.assertTrue(report['print_jobs'][0]['approved'])

    def test_subprocess_startup_to_report_under_15_seconds(self):
        # Same bound measured from a fresh process, mirroring the print station.
        source = self.write('big2.json', self.chain_input())
        output = str(self.dir / 'report2.json')
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, '-m', 'recipe_yield', 'label-preflight',
             '--source', source, '--output', output],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        elapsed = time.perf_counter() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 15.0)
        report = json.loads(Path(output).read_text())
        self.assertEqual(report['orders'][0]['evidence'][0]['occurrences'],
                         '8796093022208')


class DeclarationSemanticsTests(PreflightCase):
    def test_longest_prefix_wins_and_array_order_is_irrelevant(self):
        # A suppresses the whole first tray, then restores the base leaf; the
        # same declarations in reverse order must give the identical answer.
        forward = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'},
            {'order_id': 'A', 'path': ['tray:0', 'mix:0'], 'tag': 'local', 'action': 'restore'}]
        reverse = list(reversed(forward))
        r1 = run(catalog(forward, [job(['local'])]))['report']
        r2 = run(catalog(reverse, [job(['local'])]))['report']
        self.assertEqual(r1, r2)
        self.assertEqual(r1['orders'][0]['evidence'][0]['net'], '6')

    def test_restore_cannot_add_tag_to_ingredient_without_it(self):
        # plain carries no local tag; a restore on the plain leaf must not
        # invent the tag, while a sibling restore keeps the catalog rule.
        declarations = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'},
            {'order_id': 'A', 'path': ['tray:0', 'mix:1'], 'tag': 'local', 'action': 'restore'}]
        report = run(catalog(declarations, [job([])]))['report']
        evidence = report['orders'][0]['evidence'][0]
        # The restored plain leaf carries no local tag, so only the second
        # tray's base leaf survives the suppression (3g, one occurrence).
        self.assertEqual(evidence['net'], '3')
        self.assertEqual(evidence['occurrences'], '1')

    def test_suppress_scoped_to_one_shared_subrecipe_occurrence(self):
        # Suppressing the base leaf inside tray:0/mix:0 must not touch the
        # other three shared occurrences of mix.
        declarations = [
            {'order_id': 'A', 'path': ['tray:0', 'mix:0'], 'tag': 'local',
             'action': 'suppress'}]
        report = run(catalog(declarations, [job(['local'])]))['report']
        evidence = report['orders'][0]['evidence'][0]
        # Each mix carries the local base only at its :0 component (the :1
        # component is plain, untagged). Suppressing the base leaf inside the
        # first mix removes one of the two base leaves, so the second tray's
        # 3g base leaf is the sole survivor; the other three shared mix
        # occurrences behave exactly as before.
        self.assertEqual(evidence['net'], '3')
        self.assertEqual(evidence['occurrences'], '1')
        self.assertEqual(evidence['first_path'], ['tray:1', 'mix:0'])

    def test_suppress_on_subrecipe_node_propagates_to_every_descendant_leaf(self):
        # A declaration on the sub-recipe component tray:0 must disable the
        # tag on every leaf beneath that single occurrence, while the other
        # shared occurrence of tray stays fully catalog-driven.
        nested = {
            'ingredients': [
                {'id': 'base', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': ['local']},
                {'id': 'plain', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': []}],
            'recipes': [
                {'id': 'mix', 'output_grams': '4', 'components': [
                    {'kind': 'ingredient', 'ref': 'base', 'net_grams': '3'},
                    {'kind': 'ingredient', 'ref': 'plain', 'net_grams': '1'}]},
                {'id': 'tray', 'output_grams': '8', 'components': [
                    {'kind': 'recipe', 'ref': 'mix', 'net_grams': '4'},
                    {'kind': 'recipe', 'ref': 'mix', 'net_grams': '4'}]}],
            'orders': [{'id': 'A', 'recipe': 'tray', 'output_grams': '8'}],
            'thresholds': [{'tag': 'local', 'min_share': '0.75'}],
            'declarations': [
                {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local',
                 'action': 'suppress'}],
            'print_jobs': [{'id': 'P', 'order_ids': ['A'], 'tags': []}],
        }
        report = run(nested)['report']
        evidence = report['orders'][0]['evidence'][0]
        # Only tray:1's base leaf remains: 3g, one occurrence.
        self.assertEqual(evidence['net'], '3')
        self.assertEqual(evidence['occurrences'], '1')
        self.assertEqual(evidence['first_path'], ['tray:1', 'mix:0'])
        # 3/8 is below 0.75, so nothing is required.
        self.assertEqual(report['orders'][0]['required_tags'], [])

    def test_duplicate_declaration_rejected(self):
        one = {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local',
               'action': 'suppress'}
        with self.assertRaisesRegex(ValueError, 'duplicate declaration'):
            run(catalog([one, dict(one)], [job(['local'])]))

    def test_unknown_order_and_tagless_threshold_rejected(self):
        bad = catalog([], [job(['local'])])
        bad['declarations'] = [{'order_id': 'ghost', 'path': ['tray:0'],
                                'tag': 'local', 'action': 'suppress'}]
        with self.assertRaisesRegex(ValueError, 'unknown order'):
            run(bad)
        bad = catalog([], [job(['local'])])
        bad['declarations'] = [{'order_id': 'A', 'path': ['tray:0'],
                                'tag': 'absent', 'action': 'suppress'}]
        with self.assertRaisesRegex(ValueError, 'no threshold'):
            run(bad)


class ThresholdAndMultiTagTests(PreflightCase):
    def multi_input(self, min_share):
        return {
            'ingredients': [
                {'id': 'one', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': ['a', 'b']},
                {'id': 'two', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': ['b']},
                {'id': 'three', 'yield': '1', 'price_per_gross_gram': '1',
                 'tags': []}],
            'recipes': [{'id': 'dish', 'output_grams': '3', 'components': [
                {'kind': 'ingredient', 'ref': 'one', 'net_grams': '1'},
                {'kind': 'ingredient', 'ref': 'two', 'net_grams': '1'},
                {'kind': 'ingredient', 'ref': 'three', 'net_grams': '1'}]}],
            'orders': [{'id': 'o', 'recipe': 'dish', 'output_grams': '3'}],
            'thresholds': [
                {'tag': 'a', 'min_share': min_share},
                {'tag': 'b', 'min_share': min_share},
                {'tag': 'c', 'min_share': min_share}],
            'declarations': [],
            'print_jobs': [job([], order_ids=('o',))],
        }

    def test_exact_threshold_inclusive_and_independent_tags(self):
        # The one-third share reaches an exactly-equal threshold (inclusive).
        report = run(self.multi_input('0.333333'))['report']
        order = report['orders'][0]
        self.assertEqual(order['total_net'], '3')
        by_tag = {row['tag']: row for row in order['evidence']}
        self.assertEqual([row['tag'] for row in order['evidence']], ['a', 'b', 'c'])
        self.assertEqual(by_tag['a']['net'], '1')
        self.assertEqual(by_tag['a']['occurrences'], '1')
        self.assertEqual(by_tag['b']['net'], '2')
        self.assertEqual(by_tag['b']['occurrences'], '2')
        # Zero-contribution threshold tag is still listed, with null first path.
        self.assertEqual(by_tag['c'], {'tag': 'c', 'net': '0', 'occurrences': '0',
                                       'first_path': None})
        self.assertEqual(order['required_tags'], ['a', 'b'])

    def test_share_just_below_threshold_not_required(self):
        report = run(self.multi_input('0.34'))['report']
        order = report['orders'][0]
        # a contributes exactly 1/3 < 0.34; b contributes 2/3.
        self.assertEqual(order['required_tags'], ['b'])

    def test_threshold_tag_need_not_exist_in_catalog(self):
        report = run(self.multi_input('0.5'))['report']
        self.assertEqual(report['orders'][0]['evidence'][2]['first_path'], None)

    def test_fractional_weights_emit_reduced_rational(self):
        data = {
            'ingredients': [
                {'id': 'one', 'yield': '1', 'price_per_gross_gram': '1', 'tags': ['a']},
                {'id': 'two', 'yield': '1', 'price_per_gross_gram': '1', 'tags': []}],
            'recipes': [{'id': 'dish', 'output_grams': '2', 'components': [
                {'kind': 'ingredient', 'ref': 'one', 'net_grams': '1'},
                {'kind': 'ingredient', 'ref': 'two', 'net_grams': '1'}]}],
            'orders': [{'id': 'o', 'recipe': 'dish', 'output_grams': '3'}],
            'thresholds': [{'tag': 'a', 'min_share': '0.6'}],
            'declarations': [],
            'print_jobs': [job([], order_ids=('o',))],
        }
        report = run(data)['report']
        evidence = report['orders'][0]['evidence'][0]
        self.assertEqual(evidence['net'], '3/2')
        self.assertEqual(report['orders'][0]['total_net'], '3')


class PrintJobAndValidationTests(PreflightCase):
    def test_stale_plate_tag_appears_as_extra(self):
        # A tag absent from the threshold table is legal on a physical plate.
        # Suppress local on every leaf so neither order requires it; both the
        # current and the retired plate tag then show up as extra.
        declarations = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'},
            {'order_id': 'A', 'path': ['tray:1'], 'tag': 'local', 'action': 'suppress'}]
        report = run(catalog(declarations, [job(['local', 'retired'])]))['report']
        row = report['print_jobs'][0]['orders'][0]
        self.assertEqual(row['extra'], ['local', 'retired'])
        self.assertFalse(report['print_jobs'][0]['approved'])

    def test_union_of_required_tags_does_not_approve_job(self):
        # Both orders require local; a plate with local plus per-order
        # mismatches must still be rejected (per-order, not union, matching).
        declarations = [
            {'order_id': 'A', 'path': ['tray:0'], 'tag': 'local', 'action': 'suppress'}]
        report = run(catalog(declarations, [job(['local'])]))['report']
        self.assertFalse(report['print_jobs'][0]['approved'])

    def test_orders_must_be_covered_exactly_once(self):
        # An order missing from every job fails.
        data = catalog([], [job(['local'], order_ids=('A',))])
        with self.assertRaisesRegex(ValueError, 'every order'):
            run(data)
        # An order listed twice fails.
        data = catalog([], [
            job(['local'], job_id='J1', order_ids=('A', 'B')),
            job([], job_id='J2', order_ids=('B',))])
        with self.assertRaisesRegex(ValueError, 'more than one'):
            run(data)

    def test_print_job_identity_and_reference_checks(self):
        data = catalog([], [job(['local'], job_id='P1'), job([], job_id='P1')])
        with self.assertRaisesRegex(ValueError, 'duplicate print job'):
            run(data)
        data = catalog([], [job(['local'], order_ids=('A', 'ghost'))])
        with self.assertRaisesRegex(ValueError, 'unknown order'):
            run(data)

    def test_threshold_boundaries(self):
        for share in ('0', '1.1', '-0.5'):
            data = catalog([], [job(['local'])],
                           thresholds=[{'tag': 'local', 'min_share': share}])
            with self.subTest(share=share):
                with self.assertRaises(ValueError):
                    run(data)
        data = catalog([], [job(['local'])],
                       thresholds=[{'tag': 'x', 'min_share': '1'},
                                   {'tag': 'x', 'min_share': '0.5'}])
        with self.assertRaisesRegex(ValueError, 'duplicate threshold'):
            run(data)

    def test_strict_fields_and_types(self):
        good = catalog([], [job(['local'])])
        variants = []
        bad = json.loads(json.dumps(good))
        bad['print_jobs'][0]['extra'] = 1
        variants.append(bad)
        bad = json.loads(json.dumps(good))
        bad['thresholds'][0]['min_share'] = 0.75
        variants.append(bad)
        bad = json.loads(json.dumps(good))
        bad['declarations'] = [{'order_id': 'A', 'path': ['tray:0'],
                                'tag': 'local', 'action': 'freeze'}]
        variants.append(bad)
        bad = json.loads(json.dumps(good))
        bad['print_jobs'][0]['tags'] = ['local', 'local']
        variants.append(bad)
        for variant in variants:
            with self.assertRaises(ValueError):
                run(variant)

    def test_duplicate_json_keys_and_overwrite_rejected_by_cli(self):
        raw = '{"ingredients": [], "ingredients": []}'
        source = str(self.dir / 'dup.json')
        Path(source).write_text(raw)
        output = str(self.dir / 'out.json')
        code, out, err = self.run_cli(
            ['label-preflight', '--source', source, '--output', output])
        self.assert_rejected(code, out, err, 'duplicate JSON key')
        self.assertFalse(Path(output).exists())
        # A valid run publishes once; a second run must not overwrite it.
        good = self.write('good.json', catalog([], [job(['local'])]))
        code, _, err = self.run_cli(
            ['label-preflight', '--source', good, '--output', output])
        self.assertEqual(code, 0, err)
        before = Path(output).read_bytes()
        code, out, err = self.run_cli(
            ['label-preflight', '--source', good, '--output', output])
        self.assert_rejected(code, out, err, 'error')
        self.assertEqual(Path(output).read_bytes(), before)

    def test_no_half_report_on_invalid_input(self):
        data = catalog([], [job(['local'])])
        data['print_jobs'] = []
        source = self.write('invalid.json', data)
        output = str(self.dir / 'never.json')
        code, out, err = self.run_cli(
            ['label-preflight', '--source', source, '--output', output])
        self.assertEqual(code, 2)
        self.assertFalse(Path(output).exists())
        self.assertEqual(list(self.dir.glob('.publish-*')), [])

    def test_report_sorting_and_shapes(self):
        # Two valid jobs over disjoint orders, declared out of ASCII order.
        data = catalog([], [
            {'id': 'z', 'order_ids': ['B'], 'tags': ['local']},
            {'id': 'a', 'order_ids': ['A'], 'tags': ['local']}])
        report = run(data)['report']
        self.assertEqual([j['id'] for j in report['print_jobs']], ['a', 'z'])
        self.assertEqual([o['id'] for o in report['orders']], ['A', 'B'])
        self.assertEqual([row['order_id'] for row in report['print_jobs'][0]['orders']],
                         ['A'])


class FixtureCliTests(PreflightCase):
    def test_committed_fixture_end_to_end(self):
        source = str(ROOT / 'fixtures' / 'preflight.json')
        output = str(self.dir / 'preflight.json')
        code, out, err = self.run_cli(
            ['label-preflight', '--source', source, '--output', output])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out),
                         {'approved': 1, 'orders': 2, 'print_jobs': 1})
        report = json.loads(Path(output).read_text())
        for order in report['orders']:
            self.assertEqual(order['total_net'], '8')
            self.assertEqual(order['required_tags'], ['local'])
            self.assertEqual(order['evidence'][0],
                             {'tag': 'local', 'net': '6', 'occurrences': '2',
                              'first_path': ['tray:0', 'mix:0']})
        self.assertTrue(report['print_jobs'][0]['approved'])


class LegacyCompatibilityTests(PreflightCase):
    def test_legacy_command_and_contract_unchanged(self):
        source = str(ROOT / 'fixtures' / 'input.json')
        output = str(self.dir / 'legacy.json')
        code, out, err = self.run_cli(['--source', source, '--output', output])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'cost': '16', 'ingredients': 2, 'orders': 1})
        report = json.loads(Path(output).read_text())
        self.assertEqual(report['orders'][0]['declarations'], ['milk', 'plant'])
        self.assertEqual(report['orders'][0]['evidence'][0]['path'],
                         ['soup:0', 'sauce:0'])
        # Legacy input without the new arrays still parses for the old command.
        self.assertEqual(code, 0)

    def test_preflight_rejects_input_missing_new_arrays(self):
        legacy = json.loads(Path(ROOT / 'fixtures' / 'input.json').read_text())
        with self.assertRaises(ValueError):
            parse_preflight(legacy)

    def test_stock_commands_still_route(self):
        code, out, err = self.run_cli(['stock-show', '--ledger', str(self.dir / 'noledger')])
        self.assert_rejected(code, out, err, 'ledger not found')


if __name__ == '__main__':
    unittest.main()
