import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'src'))
from recipe_yield import cli

def catalog(orders,restrictions,request_id='req-1',use_on='2027-01-01'):
    return {'ingredients':[{'id':'flour','yield':'0.75','price_per_gross_gram':'1','tags':['catalog']}],
            'recipes':[{'id':'sauce','output_grams':'6','components':[{'kind':'ingredient','ref':'flour','net_grams':'6'}]},
                       {'id':'soup','output_grams':'12','components':[{'kind':'recipe','ref':'sauce','net_grams':'6'},
                                                                     {'kind':'recipe','ref':'sauce','net_grams':'6'}]}],
            'orders':orders,'request_id':request_id,'use_on':use_on,'restrictions':restrictions}

def lunch():return {'id':'lunch','recipe':'soup','output_grams':'12'}

def restrict_second():return [{'order_id':'lunch','path':['soup:1','sauce:0'],'forbidden_tags':['shared']}]

def lot(lot_id,expires,price,tags,gross='8',ingredient='flour'):
    return {'lot_id':lot_id,'ingredient':ingredient,'expires_on':expires,'gross_grams':gross,
            'price_per_gross_gram':price,'tags':tags}

def lots_abc():
    return {'lots':[lot('A','2027-01-01','2',['plant']),lot('B','2027-01-02','3',['shared']),
                    lot('C','2027-01-03','4',['reserve'])]}

class StockCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
    def write(self,name,obj):
        path=self.dir/name;path.write_text(json.dumps(obj));return str(path)
    def write_raw(self,name,text):
        path=self.dir/name;path.write_text(text);return str(path)
    def run_cli(self,argv):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=cli.main(argv)
        return code,out.getvalue(),err.getvalue()
    def init_ledger(self,name,lots):
        led=str(self.dir/name)
        code,out,err=self.run_cli(['stock-init','--ledger',led,'--source',self.write(name+'.json',lots)])
        self.assertEqual(code,0,err);self.assertEqual(out,'')
        return led
    def issue(self,led,source):
        return self.run_cli(['stock-issue','--ledger',led,'--source',source])
    def show(self,led):
        code,out,err=self.run_cli(['stock-show','--ledger',led])
        self.assertEqual(code,0,err)
        return json.loads(out)
    def balances(self,led):
        return {row['lot_id']:row['gross_remaining'] for row in self.show(led)['lots']}
    def assert_rejected(self,code,out,err,*fragments):
        self.assertEqual(code,2,(out,err));self.assertEqual(out,'')
        for fragment in fragments:self.assertIn(fragment,err)

class ScenarioOneTests(StockCase):
    def test_restricted_allocation(self):
        led=self.init_ledger('led',lots_abc())
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],restrict_second())))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        self.assertEqual(r['request_id'],'req-1');self.assertEqual(r['use_on'],'2027-01-01')
        self.assertEqual(r['total_cost'],'40')
        first,second=r['lines']
        self.assertEqual(first['order_id'],'lunch');self.assertEqual(first['path'],['soup:0','sauce:0'])
        self.assertEqual(first['ingredient'],'flour')
        self.assertEqual((first['net'],first['gross'],first['trim'],first['cost']),('6','8','2','24'))
        self.assertEqual(first['tags'],['shared'])
        self.assertEqual(first['draws'],[{'lot_id':'B','gross':'8','cost':'24'}])
        self.assertEqual(second['path'],['soup:1','sauce:0'])
        self.assertEqual((second['net'],second['gross'],second['trim'],second['cost']),('6','8','2','16'))
        self.assertEqual(second['tags'],['plant'])
        self.assertEqual(second['draws'],[{'lot_id':'A','gross':'8','cost':'16'}])
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'8'})
    def test_without_reserve_lot_allocation_unchanged(self):
        # Only A and B exist: the first leaf must not grab A and starve the
        # restricted second leaf; the allocation matches the three-lot case.
        lots={'lots':lots_abc()['lots'][:2]}
        led=self.init_ledger('led',lots)
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],restrict_second())))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        self.assertEqual([list(d['lot_id'] for d in line['draws']) for line in r['lines']],[['B'],['A']])
        self.assertEqual(r['total_cost'],'40')
        self.assertEqual(self.balances(led),{'A':'0','B':'0'})
    def test_without_restriction_earliest_lot_first(self):
        led=self.init_ledger('led',lots_abc())
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],[])))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        self.assertEqual([list(d['lot_id'] for d in line['draws']) for line in r['lines']],[['A'],['B']])
        self.assertEqual(r['total_cost'],'40')
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'8'})
    def test_later_use_date_expires_first_lot(self):
        led=self.init_ledger('led',lots_abc())
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],restrict_second(),use_on='2027-01-02')))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        self.assertEqual([list(d['lot_id'] for d in line['draws']) for line in r['lines']],[['B'],['C']])
        self.assertEqual(r['total_cost'],'56')
        self.assertEqual(self.balances(led),{'A':'8','B':'0','C':'0'})
    def test_legacy_report_unaffected(self):
        # The original command still prices the catalog theoretically and
        # never touches the ledger.
        led=self.init_ledger('led',lots_abc())
        data=catalog([lunch()],restrict_second())
        legacy={'ingredients':data['ingredients'],'recipes':data['recipes'],'orders':data['orders']}
        src=self.write('legacy.json',legacy);out_path=str(self.dir/'report.json')
        code,out,err=self.run_cli(['--source',src,'--output',out_path])
        self.assertEqual(code,0,err)
        report=json.loads(Path(out_path).read_text())
        self.assertEqual(report['summary']['cost'],'16')
        self.assertEqual(report['orders'][0]['declarations'],['catalog'])
        self.assertEqual([line['path'] for line in report['orders'][0]['evidence']],
                         [['soup:0','sauce:0'],['soup:1','sauce:0']])
        self.assertEqual(self.balances(led),{'A':'8','B':'8','C':'8'})

class ScenarioTwoTests(StockCase):
    def commit_first(self):
        led=self.init_ledger('led',lots_abc())
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],restrict_second())))
        self.assertEqual(code,0,err)
        return led,out
    def test_failed_request_id_stays_reusable(self):
        led,_=self.commit_first()
        # A new id asking for the same 16g gross must be rejected (only C=8
        # remains) and must not occupy its id.
        src=self.write('again.json',catalog([lunch()],restrict_second(),request_id='req-2'))
        code,out,err=self.issue(led,src)
        self.assert_rejected(code,out,err,'insufficient stock')
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'8'})
        # The same id may be reused for a different, feasible request.
        small=catalog([{'id':'snack','recipe':'sauce','output_grams':'6'}],[],request_id='req-2')
        code,out,err=self.issue(led,self.write('small.json',small))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        self.assertEqual(r['total_cost'],'32')
        self.assertEqual(r['lines'][0]['draws'],[{'lot_id':'C','gross':'8','cost':'32'}])
        self.assertEqual(r['lines'][0]['tags'],['reserve'])
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'0'})
    def test_retry_returns_original_receipt_without_deduction(self):
        led,first_out=self.commit_first()
        # Drain the rest, then retry: the stored receipt must come back
        # byte-identically and balances must not move.
        small=catalog([{'id':'snack','recipe':'sauce','output_grams':'6'}],[],request_id='req-2')
        code,_,err=self.issue(led,self.write('small.json',small));self.assertEqual(code,0,err)
        code,out,err=self.issue(led,self.write('issue.json',catalog([lunch()],restrict_second())))
        self.assertEqual(code,0,err)
        self.assertEqual(out,first_out)
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'0'})
    def test_same_id_different_content_rejected(self):
        led,_=self.commit_first()
        changed=catalog([lunch()],restrict_second(),use_on='2027-01-03')
        code,out,err=self.issue(led,self.write('changed.json',changed))
        self.assert_rejected(code,out,err,'request_id conflict')
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'8'})
    def test_decimal_respelling_is_different_content(self):
        led,first_out=self.commit_first()
        raw=json.dumps(catalog([lunch()],restrict_second())).replace('"output_grams": "12"','"output_grams": "12.0"')
        code,out,err=self.issue(led,self.write_raw('respelled.json',raw))
        self.assert_rejected(code,out,err,'request_id conflict')
    def test_key_order_and_whitespace_are_same_content(self):
        led,first_out=self.commit_first()
        obj=catalog([lunch()],restrict_second())
        reordered={'restrictions':obj['restrictions'],'use_on':obj['use_on'],'request_id':obj['request_id'],
                   'orders':obj['orders'],'recipes':obj['recipes'],'ingredients':obj['ingredients']}
        code,out,err=self.issue(led,self.write_raw('reordered.json',json.dumps(reordered,indent=4)))
        self.assertEqual(code,0,err)
        self.assertEqual(out,first_out)
        self.assertEqual(self.balances(led),{'A':'0','B':'0','C':'8'})
    def test_split_draw_across_lots(self):
        lots={'lots':[lot('L1','2027-01-01','2',[],gross='1'),lot('L2','2027-01-02','3',[],gross='1')]}
        led=self.init_ledger('led',lots)
        data={'ingredients':[{'id':'flour','yield':'0.75','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
              'orders':[{'id':'o','recipe':'dish','output_grams':'1'}],
              'request_id':'split-1','use_on':'2027-01-01','restrictions':[]}
        code,out,err=self.issue(led,self.write('split.json',data))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        line=r['lines'][0]
        self.assertEqual((line['net'],line['gross'],line['trim'],line['cost']),('1','4/3','1/3','3'))
        self.assertEqual(line['draws'],[{'lot_id':'L1','gross':'1','cost':'2'},
                                        {'lot_id':'L2','gross':'1/3','cost':'1'}])
        self.assertEqual(r['total_cost'],'3')
        self.assertEqual(self.balances(led),{'L1':'0','L2':'2/3'})

class ConcurrencyTests(StockCase):
    def proc_args(self):return [sys.executable,'-m','recipe_yield']
    def proc_env(self,extra=None):
        env=dict(os.environ);env['PYTHONPATH']=str(ROOT/'src')
        if extra:env.update(extra)
        return env
    def run_proc(self,args,env_extra=None):
        return subprocess.run(self.proc_args()+args,capture_output=True,env=self.proc_env(env_extra))
    def start_proc(self,args,env_extra=None):
        return subprocess.Popen(self.proc_args()+args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.proc_env(env_extra))
    def test_contending_requests_exactly_one_succeeds(self):
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','2',[])]})
        def demand(rid):
            return {'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
                    'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
                    'orders':[{'id':'o','recipe':'dish','output_grams':'8'}],
                    'request_id':rid,'use_on':'2027-01-01','restrictions':[]}
        src1=self.write('r1.json',demand('race-1'));src2=self.write('r2.json',demand('race-2'))
        p1=self.start_proc(['stock-issue','--ledger',led,'--source',src1])
        p2=self.start_proc(['stock-issue','--ledger',led,'--source',src2])
        out1,err1=p1.communicate();out2,err2=p2.communicate()
        self.assertEqual(sorted([p1.returncode,p2.returncode]),[0,2],(err1,err2))
        self.assertEqual(self.balances(led),{'A':'0'})
    def test_identical_concurrent_requests_deduct_once(self):
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','2',[])]})
        def demand(rid):
            return {'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
                    'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
                    'orders':[{'id':'o','recipe':'dish','output_grams':'8'}],
                    'request_id':rid,'use_on':'2027-01-01','restrictions':[]}
        src=self.write('r.json',demand('race-1'))
        p1=self.start_proc(['stock-issue','--ledger',led,'--source',src])
        p2=self.start_proc(['stock-issue','--ledger',led,'--source',src])
        out1,err1=p1.communicate();out2,err2=p2.communicate()
        self.assertEqual((p1.returncode,p2.returncode),(0,0),(err1,err2))
        self.assertEqual(out1,out2)
        self.assertEqual(self.balances(led),{'A':'0'})
    def test_crash_before_commit_leaves_no_trace(self):
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','2',[])]})
        data={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
              'orders':[{'id':'o','recipe':'dish','output_grams':'8'}],
              'request_id':'crash-1','use_on':'2027-01-01','restrictions':[]}
        src=self.write('c.json',data)
        result=self.run_proc(['stock-issue','--ledger',led,'--source',src],{'RECIPE_YIELD_CRASHPOINT':'before_commit'})
        self.assertEqual(result.returncode,99)
        self.assertEqual(result.stdout,b'')
        self.assertEqual(self.balances(led),{'A':'8'})
        # The id was not occupied: a fresh process can commit it.
        result=self.run_proc(['stock-issue','--ledger',led,'--source',src])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.balances(led),{'A':'0'})
    def test_crash_after_commit_recovers_receipt(self):
        lots={'lots':[lot('A','2027-01-01','2',[]),lot('B','2027-01-02','3',[])]}
        led=self.init_ledger('led',lots)
        led_ref=self.init_ledger('ledref',lots)
        data={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
              'orders':[{'id':'o','recipe':'dish','output_grams':'8'}],
              'request_id':'crash-2','use_on':'2027-01-01','restrictions':[]}
        src=self.write('c.json',data)
        result=self.run_proc(['stock-issue','--ledger',led,'--source',src],{'RECIPE_YIELD_CRASHPOINT':'after_commit'})
        self.assertEqual(result.returncode,99)
        self.assertEqual(result.stdout,b'')
        # Committed despite the missing receipt.
        self.assertEqual(self.balances(led),{'A':'0','B':'8'})
        # A later request consumes the remaining balance.
        rest=dict(data);rest['request_id']='next-1'
        src2=self.write('n.json',rest)
        result=self.run_proc(['stock-issue','--ledger',led,'--source',src2])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.balances(led),{'A':'0','B':'0'})
        # The crashed id still returns its original receipt, byte-identical
        # to a clean run, without deducting again.
        result=self.run_proc(['stock-issue','--ledger',led,'--source',src])
        self.assertEqual(result.returncode,0,result.stderr)
        reference=self.run_proc(['stock-issue','--ledger',led_ref,'--source',src])
        self.assertEqual(reference.returncode,0,reference.stderr)
        self.assertEqual(result.stdout,reference.stdout)
        self.assertEqual(self.balances(led),{'A':'0','B':'0'})

class ValidationTests(StockCase):
    def test_init_rejects_existing_path(self):
        led=self.init_ledger('led',{'lots':[]})
        code,out,err=self.run_cli(['stock-init','--ledger',led,'--source',self.write('lots.json',{'lots':[]})])
        self.assert_rejected(code,out,err,'already exists')
    def test_init_lot_validation(self):
        bad=[{'lot_id':'A','ingredient':'flour','expires_on':'2027-01-01','gross_grams':'0','price_per_gross_gram':'1','tags':[]},
             {'lot_id':'A','ingredient':'flour','expires_on':'2027-01-01','gross_grams':'1','price_per_gross_gram':'-1','tags':[]},
             {'lot_id':'A','ingredient':'flour','expires_on':'2027-02-30','gross_grams':'1','price_per_gross_gram':'1','tags':[]},
             {'lot_id':'A','ingredient':'flour','expires_on':'2027-01-01','gross_grams':'1','price_per_gross_gram':'1','tags':['x','x']},
             {'lot_id':'A','ingredient':'flour','expires_on':'2027-01-01','gross_grams':'1','price_per_gross_gram':'1','tags':['x']*0,'extra':1}]
        for item in bad:
            code,out,err=self.run_cli(['stock-init','--ledger',str(self.dir/'x'),'--source',self.write('bad.json',{'lots':[item]})])
            self.assert_rejected(code,out,err,'error')
        dup={'lots':[lot('A','2027-01-01','1',[],gross='1'),lot('A','2027-01-02','1',[],gross='1')]}
        code,out,err=self.run_cli(['stock-init','--ledger',str(self.dir/'y'),'--source',self.write('dup.json',dup)])
        self.assert_rejected(code,out,err,'error')
        many={'lots':[lot(f'L{i:03d}','2027-01-01','1',[],gross='1') for i in range(201)]}
        code,out,err=self.run_cli(['stock-init','--ledger',str(self.dir/'z'),'--source',self.write('many.json',many)])
        self.assert_rejected(code,out,err,'error')
    def test_show_missing_and_corrupt_ledger(self):
        code,out,err=self.run_cli(['stock-show','--ledger',str(self.dir/'absent')])
        self.assert_rejected(code,out,err,'ledger not found')
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','1',[],gross='1')]})
        (Path(led)/'state.json').write_text('{corrupt')
        code,out,err=self.run_cli(['stock-show','--ledger',led])
        self.assert_rejected(code,out,err,'cannot safely read ledger')
    def test_issue_missing_ledger(self):
        src=self.write('issue.json',catalog([lunch()],[]))
        code,out,err=self.run_cli(['stock-issue','--ledger',str(self.dir/'absent'),'--source',src])
        self.assert_rejected(code,out,err,'ledger not found')
    def test_duplicate_json_keys_rejected(self):
        raw='{"lots": [], "lots": []}'
        code,out,err=self.run_cli(['stock-init','--ledger',str(self.dir/'x'),'--source',self.write_raw('dup.json',raw)])
        self.assert_rejected(code,out,err,'duplicate JSON key')
        led=self.init_ledger('led',{'lots':[]})
        raw=json.dumps(catalog([lunch()],[]))[:-1]+',"use_on":"2027-01-01"}'
        code,out,err=self.issue(led,self.write_raw('dup2.json',raw))
        self.assert_rejected(code,out,err,'duplicate JSON key')
    def test_unknown_fields_and_types(self):
        led=self.init_ledger('led',lots_abc())
        data=catalog([lunch()],[]);data['extra']=1
        code,out,err=self.issue(led,self.write('u1.json',data))
        self.assert_rejected(code,out,err,'error')
        data=catalog([lunch()],[]);data['orders'][0]['output_grams']=12
        code,out,err=self.issue(led,self.write('u2.json',data))
        self.assert_rejected(code,out,err,'error')
        data=catalog([lunch()],[]);data['request_id']='bad id!'
        code,out,err=self.issue(led,self.write('u3.json',data))
        self.assert_rejected(code,out,err,'error')
        data=catalog([lunch()],[],use_on='2027-13-01')
        code,out,err=self.issue(led,self.write('u4.json',data))
        self.assert_rejected(code,out,err,'invalid date')
    def test_restriction_reference_errors(self):
        led=self.init_ledger('led',lots_abc())
        data=catalog([lunch()],[{'order_id':'ghost','path':['soup:0','sauce:0'],'forbidden_tags':['x']}])
        code,out,err=self.issue(led,self.write('r1.json',data))
        self.assert_rejected(code,out,err,'unknown order')
        # A path that ends at the sub-recipe node is not a leaf.
        data=catalog([lunch()],[{'order_id':'lunch','path':['soup:1'],'forbidden_tags':['shared']}])
        code,out,err=self.issue(led,self.write('r2.json',data))
        self.assert_rejected(code,out,err,'not a leaf')
        data=catalog([lunch()],[{'order_id':'lunch','path':['soup:9','sauce:0'],'forbidden_tags':['shared']}])
        code,out,err=self.issue(led,self.write('r3.json',data))
        self.assert_rejected(code,out,err,'not a leaf')
        data=catalog([lunch()],restrict_second()*2)
        code,out,err=self.issue(led,self.write('r4.json',data))
        self.assert_rejected(code,out,err,'duplicate restriction')
        data=catalog([lunch()],[{'order_id':'lunch','path':['soup:0','sauce:0'],'forbidden_tags':[]}])
        code,out,err=self.issue(led,self.write('r5.json',data))
        self.assert_rejected(code,out,err,'error')
    def test_leaf_limit(self):
        components=[{'kind':'ingredient','ref':'flour','net_grams':'1'} for _ in range(30)]
        data={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'r1','output_grams':'30','components':components},
                         {'id':'r2','output_grams':'900','components':[{'kind':'recipe','ref':'r1','net_grams':'30'} for _ in range(30)]}],
              'orders':[{'id':'big','recipe':'r2','output_grams':'900'}],
              'request_id':'big-1','use_on':'2027-01-01','restrictions':[]}
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','1',[],gross='999999')]})
        code,out,err=self.issue(led,self.write('big.json',data))
        self.assert_rejected(code,out,err,'leaf limit')
    def test_exactly_200_leaves_accepted(self):
        data={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'r1','output_grams':'10','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'} for _ in range(10)]},
                         {'id':'r2','output_grams':'200','components':[{'kind':'recipe','ref':'r1','net_grams':'10'} for _ in range(20)]}],
              'orders':[{'id':'big','recipe':'r2','output_grams':'200'}],
              'request_id':'big-2','use_on':'2027-01-01','restrictions':[]}
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','1',[],gross='200')]})
        code,out,err=self.issue(led,self.write('ok.json',data))
        self.assertEqual(code,0,err)
        self.assertEqual(len(json.loads(out)['lines']),200)
        self.assertEqual(self.balances(led),{'A':'0'})
    def test_expiry_and_tag_rules(self):
        # Expiry-day lot is usable; one day later it is not.
        lots={'lots':[lot('A','2027-01-01','2',[])]}
        led=self.init_ledger('led',lots)
        demand={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
                'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
                'orders':[{'id':'o','recipe':'dish','output_grams':'8'}],
                'request_id':'d-1','use_on':'2027-01-01','restrictions':[]}
        code,out,err=self.issue(led,self.write('d1.json',demand))
        self.assertEqual(code,0,err)
        led2=self.init_ledger('led2',lots)
        demand['request_id']='d-2';demand['use_on']='2027-01-02'
        code,out,err=self.issue(led2,self.write('d2.json',demand))
        self.assert_rejected(code,out,err,'insufficient stock')
        # Forbidding the only tag blocks the lot.
        led3=self.init_ledger('led3',{'lots':[lot('A','2027-01-01','2',['shared'])]})
        demand['request_id']='d-3';demand['use_on']='2027-01-01'
        demand['restrictions']=[{'order_id':'o','path':['dish:0'],'forbidden_tags':['shared']}]
        code,out,err=self.issue(led3,self.write('d3.json',demand))
        self.assert_rejected(code,out,err,'insufficient stock')
        # Wrong ingredient never matches.
        led4=self.init_ledger('led4',{'lots':[lot('A','2027-01-01','2',[],ingredient='sugar')]})
        demand['request_id']='d-4';demand['restrictions']=[]
        code,out,err=self.issue(led4,self.write('d4.json',demand))
        self.assert_rejected(code,out,err,'insufficient stock')
    def test_show_sorts_and_keeps_zero_balances(self):
        led=self.init_ledger('led',{'lots':[lot('C','2027-01-03','1',[],gross='3'),
                                            lot('A','2027-01-01','1',[],gross='1'),
                                            lot('B','2027-01-02','1',[],gross='2')]})
        rows=self.show(led)['lots']
        self.assertEqual([r['lot_id'] for r in rows],['A','B','C'])
        demand={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
                'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
                'orders':[{'id':'o','recipe':'dish','output_grams':'1'}],
                'request_id':'z-1','use_on':'2027-01-01','restrictions':[]}
        code,out,err=self.issue(led,self.write('z.json',demand))
        self.assertEqual(code,0,err)
        rows=self.show(led)['lots']
        self.assertEqual([(r['lot_id'],r['gross_remaining']) for r in rows],[('A','0'),('B','2'),('C','3')])
    def test_empty_ledger(self):
        led=self.init_ledger('led',{'lots':[]})
        self.assertEqual(self.show(led),{'lots':[]})
        demand={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
                'recipes':[{'id':'dish','output_grams':'1','components':[{'kind':'ingredient','ref':'flour','net_grams':'1'}]}],
                'orders':[{'id':'o','recipe':'dish','output_grams':'1'}],
                'request_id':'e-1','use_on':'2027-01-01','restrictions':[]}
        code,out,err=self.issue(led,self.write('e.json',demand))
        self.assert_rejected(code,out,err,'insufficient stock')

class LeafOrderTests(StockCase):
    def test_lines_sorted_by_order_and_numeric_path(self):
        components=[{'kind':'ingredient','ref':'flour','net_grams':'1'} for _ in range(12)]
        data={'ingredients':[{'id':'flour','yield':'1','price_per_gross_gram':'1','tags':[]}],
              'recipes':[{'id':'wide','output_grams':'12','components':components}],
              'orders':[{'id':'zeta','recipe':'wide','output_grams':'3'},
                        {'id':'alpha','recipe':'wide','output_grams':'12'}],
              'request_id':'ord-1','use_on':'2027-01-01','restrictions':[]}
        led=self.init_ledger('led',{'lots':[lot('A','2027-01-01','1',[],gross='100')]})
        code,out,err=self.issue(led,self.write('ord.json',data))
        self.assertEqual(code,0,err)
        r=json.loads(out)
        keys=[(line['order_id'],line['path'][0]) for line in r['lines']]
        self.assertEqual(keys,[('alpha',f'wide:{i}') for i in range(12)]+[('zeta',f'wide:{i}') for i in range(12)])

class LegacyCliTests(StockCase):
    def test_legacy_command_roundtrip_and_no_overwrite(self):
        src=str(ROOT/'fixtures'/'input.json');out_path=str(self.dir/'report.json')
        code,out,err=self.run_cli(['--source',src,'--output',out_path])
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out),{'cost':'16','ingredients':2,'orders':1})
        before=Path(out_path).read_bytes()
        code,out,err=self.run_cli(['--source',src,'--output',out_path])
        self.assertEqual(code,2);self.assertEqual(Path(out_path).read_bytes(),before)
    def test_legacy_command_ignores_ledger(self):
        led=self.init_ledger('led',lots_abc())
        src=str(ROOT/'fixtures'/'input.json')
        code,out,err=self.run_cli(['--source',src,'--output',str(self.dir/'r.json')])
        self.assertEqual(code,0,err)
        self.assertEqual(self.balances(led),{'A':'8','B':'8','C':'8'})

if __name__=='__main__':unittest.main()
