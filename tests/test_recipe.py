import copy,json,unittest
from pathlib import Path
from recipe_yield.service import process

class RecipeTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(Path('fixtures/input.json').read_text())
    def test_nested_cost_and_purchase(self):
        r=process(self.data);self.assertEqual(r['summary']['cost'],'16')
        self.assertEqual(r['purchases'][1],{'ingredient':'tomato','net':'320','gross':'400','trim':'80','cost':'8'})
        self.assertEqual(r['orders'][0]['declarations'],['milk','plant'])
        self.assertEqual(r['orders'][0]['evidence'][0]['path'],['soup:0','sauce:0'])
    def test_scaled_orders(self):
        self.data['orders'].append({'id':'dinner','recipe':'sauce','output_grams':'50'})
        r=process(self.data);self.assertEqual(r['summary']['cost'],'18')
    def test_unused_cycle_rejected(self):
        self.data['recipes'].append({'id':'loop','output_grams':'1','components':[{'kind':'recipe','ref':'loop','net_grams':'1'}]})
        with self.assertRaisesRegex(ValueError,'cycle'):process(self.data)
    def test_bad_yield_and_mass(self):
        for mutate in (lambda d:d['ingredients'][0].update({'yield':'0'}),lambda d:d['recipes'][0].update({'output_grams':'121'})):
            d=copy.deepcopy(self.data);mutate(d)
            with self.assertRaises(ValueError):process(d)
    def test_unknown_and_duplicate(self):
        for mutate in (lambda d:d['recipes'][0]['components'][0].update({'ref':'absent'}),lambda d:d['orders'].append(copy.deepcopy(d['orders'][0]))):
            d=copy.deepcopy(self.data);mutate(d)
            with self.assertRaises(ValueError):process(d)
    def test_zero_price_and_no_tags(self):
        for i in self.data['ingredients']:i.update(price_per_gross_gram='0',tags=[])
        r=process(self.data);self.assertEqual(r['summary']['cost'],'0');self.assertEqual(r['orders'][0]['declarations'],[])
