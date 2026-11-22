import json
import math
import os
import fcntl
from fractions import Fraction
from pathlib import Path

from . import model
from .checks import fields,array,ident,unique,number,date,rational
from .expand import expand
from .flow import Dinic
from .io import pairs,constant

VERSION=1
MAX_LOTS=200
MAX_LEAVES=200
MAX_RESTRICTIONS=200

class _Number:
    # JSON number literal kept as source text so that the request fingerprint
    # can distinguish spellings such as 1 and 1.0.
    __slots__=('text',)
    def __init__(self,text):self.text=text

def _load_json(path,preserve_numbers=False):
    with open(path,'rb') as f:raw=f.read(8000001)
    if len(raw)>8000000:raise ValueError('input over 8MB')
    text=raw.decode('utf-8')
    if preserve_numbers:
        return json.loads(text,object_pairs_hook=pairs,parse_constant=constant,parse_int=_Number,parse_float=_Number)
    return json.loads(text,object_pairs_hook=pairs,parse_constant=constant)

def _unwrap(node):
    if isinstance(node,dict):return {k:_unwrap(v) for k,v in node.items()}
    if isinstance(node,list):return [_unwrap(v) for v in node]
    if isinstance(node,_Number):
        try:return int(node.text)
        except ValueError:return float(node.text)
    return node

def fingerprint(node):
    # Canonical form of the parsed request: object key order and whitespace
    # are ignored; array order, string values and number spellings are kept.
    out=[]
    def walk(n):
        if isinstance(n,dict):
            out.append('{')
            for k in sorted(n):
                out.append(json.dumps(k,ensure_ascii=False));out.append(':');walk(n[k])
            out.append('}')
        elif isinstance(n,list):
            out.append('[')
            for v in n:walk(v);out.append(',')
            out.append(']')
        elif isinstance(n,_Number):out.append('#');out.append(n.text)
        elif isinstance(n,str):out.append(json.dumps(n,ensure_ascii=False))
        elif n is True:out.append('true')
        elif n is False:out.append('false')
        elif n is None:out.append('null')
        else:raise ValueError('unsupported JSON value')
    walk(node)
    return ''.join(out)

def parse_lots(data):
    fields(data,('lots',))
    lots=[]
    for item in array(data['lots'],0,MAX_LOTS):
        fields(item,('lot_id','ingredient','expires_on','gross_grams','price_per_gross_gram','tags'))
        lot_id=ident(item['lot_id']);ingredient=ident(item['ingredient']);expires=date(item['expires_on'])
        gross=number(item['gross_grams']);price=number(item['price_per_gross_gram'])
        if gross<=0:raise ValueError('gross_grams must be positive')
        if price<0:raise ValueError('price_per_gross_gram must be non-negative')
        tags=[ident(t) for t in array(item['tags'],0,30)];unique(tags)
        lots.append({'lot_id':lot_id,'ingredient':ingredient,'expires_on':expires,
                     'gross_remaining':gross,'price':price,'tags':tags})
    unique([l['lot_id'] for l in lots])
    return lots

def _state_to_json(state):
    return {'version':VERSION,
            'lots':[{'lot_id':l['lot_id'],'ingredient':l['ingredient'],'expires_on':l['expires_on'],
                     'gross_remaining':str(l['gross_remaining']),'price_per_gross_gram':str(l['price']),
                     'tags':sorted(l['tags'])} for l in state['lots']],
            'requests':state['requests']}

def _state_from_json(data):
    if not isinstance(data,dict):raise ValueError('unsupported ledger state')
    fields(data,('version','lots','requests'))
    if data['version']!=VERSION:raise ValueError('unsupported ledger version')
    lots=[]
    for item in array(data.get('lots'),0,MAX_LOTS):
        fields(item,('lot_id','ingredient','expires_on','gross_remaining','price_per_gross_gram','tags'))
        gross=rational(item['gross_remaining']);price=rational(item['price_per_gross_gram'])
        if gross<0 or price<0:raise ValueError('negative ledger quantity')
        tags=[ident(t) for t in array(item['tags'],0,30)];unique(tags)
        lots.append({'lot_id':ident(item['lot_id']),'ingredient':ident(item['ingredient']),
                     'expires_on':date(item['expires_on']),'gross_remaining':gross,'price':price,'tags':tags})
    unique([l['lot_id'] for l in lots])
    requests=data.get('requests')
    if not isinstance(requests,dict):raise ValueError('invalid ledger requests')
    for key,value in requests.items():
        ident(key);fields(value,('fingerprint','receipt'))
        if not isinstance(value['fingerprint'],str) or not isinstance(value['receipt'],str):
            raise ValueError('invalid ledger receipt')
    return {'lots':lots,'requests':requests}

def _read_state(led):
    state_path=led/'state.json'
    if not state_path.is_file():raise ValueError('ledger state missing or unreadable')
    try:
        data=json.loads(state_path.read_bytes().decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
        return _state_from_json(data)
    except (ValueError,UnicodeError) as e:
        raise ValueError(f'cannot safely read ledger: {e}') from e

def _write_state(led,state):
    payload=(json.dumps(_state_to_json(state),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')
    for stale in led.glob('.state-*.tmp'):stale.unlink(missing_ok=True)
    temporary=led/f'.state-{os.getpid()}.tmp'
    with open(temporary,'wb') as f:
        f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(temporary,led/'state.json')
    dirfd=os.open(led,os.O_RDONLY)
    try:os.fsync(dirfd)
    finally:os.close(dirfd)

class _Lock:
    def __init__(self,led,exclusive):self.led=led;self.exclusive=exclusive;self.fd=None
    def __enter__(self):
        self.fd=os.open(self.led/'lock',os.O_RDWR|os.O_CREAT,0o666)
        fcntl.flock(self.fd,fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        return self
    def __exit__(self,*exc):
        fcntl.flock(self.fd,fcntl.LOCK_UN);os.close(self.fd);self.fd=None
        return False

def _open_ledger(path):
    led=Path(path)
    if not led.is_dir():raise ValueError('ledger not found')
    return led

def init_ledger(path,data):
    lots=parse_lots(data)
    led=Path(path)
    try:os.mkdir(led)
    except (FileExistsError,NotADirectoryError):raise ValueError('ledger path already exists') from None
    try:_write_state(led,{'lots':lots,'requests':{}})
    except (ValueError,OSError):
        # Best effort: do not leave a half-created ledger behind.
        for stale in led.glob('.state-*.tmp'):stale.unlink(missing_ok=True)
        (led/'state.json').unlink(missing_ok=True)
        try:os.rmdir(led)
        except OSError:pass
        raise

def show_ledger(path):
    led=_open_ledger(path)
    with _Lock(led,exclusive=False):
        state=_read_state(led)
    rows=[{'lot_id':l['lot_id'],'gross_remaining':str(l['gross_remaining'])} for l in state['lots']]
    rows.sort(key=lambda r:r['lot_id'])
    return {'lots':rows}

def _parse_issue(data):
    ingredients,recipes,orders=model.parse(data,extra=('request_id','use_on','restrictions'))
    request_id=ident(data['request_id'])
    use_on=date(data['use_on'])
    restrictions=[]
    for item in array(data['restrictions'],0,MAX_RESTRICTIONS):
        fields(item,('order_id','path','forbidden_tags'))
        order_id=ident(item['order_id'])
        path=array(item['path'],1,100)
        for element in path:
            if not isinstance(element,str):raise ValueError('invalid restriction path')
        forbidden=[ident(t) for t in array(item['forbidden_tags'],1,30)];unique(forbidden)
        restrictions.append({'order_id':order_id,'path':path,'forbidden_tags':forbidden})
    return ingredients,recipes,orders,request_id,use_on,restrictions

def _path_key(path):
    return tuple(int(element.rsplit(':',1)[1]) for element in path)

def _expand_leaves(ingredients,recipes,orders):
    leaves=[]
    for order_id,ref,grams in orders:
        for line in expand(ingredients,recipes,ref,grams):
            leaves.append({'order_id':order_id,'path':line['path'],'ingredient':line['ingredient'],
                           'net':line['net'],'gross':line['gross'],'forbidden':set()})
        if len(leaves)>MAX_LEAVES:raise ValueError('expanded leaf limit exceeded')
    leaves.sort(key=lambda leaf:(leaf['order_id'],_path_key(leaf['path'])))
    return leaves

def _bind_restrictions(leaves,orders,restrictions):
    order_ids={o[0] for o in orders}
    index={(leaf['order_id'],tuple(leaf['path'])):leaf for leaf in leaves}
    seen=set()
    for r in restrictions:
        if r['order_id'] not in order_ids:raise ValueError(f"unknown order in restriction: {r['order_id']}")
        key=(r['order_id'],tuple(r['path']))
        if key in seen:raise ValueError('duplicate restriction path')
        seen.add(key)
        leaf=index.get(key)
        if leaf is None:raise ValueError('restriction path is not a leaf of the order')
        leaf['forbidden']=set(r['forbidden_tags'])

def _maxflow(lot_caps,leaf_caps,edges):
    # edges: iterable of (lot_position, leaf_position); node layout is
    # source | lots | leaves | sink. Capacities are scaled integers.
    nl=len(lot_caps);nv=len(leaf_caps);sink=1+nl+nv
    d=Dinic(sink+1,zero=0)
    infinity=sum(leaf_caps)
    for p,cap in enumerate(lot_caps):
        if cap>0:d.add_edge(0,1+p,cap)
    for q,cap in enumerate(leaf_caps):
        if cap>0:d.add_edge(1+nl+q,sink,cap)
    for p,q in edges:d.add_edge(1+p,1+nl+q,infinity)
    return d

def _allocate(leaves,lots,use_on):
    # Exact lexicographically maximal allocation. Lots and leaves of
    # different ingredients never interact, so the global lexicographic
    # optimum is the interleaving of the per-ingredient optima. Returns
    # {leaf_index: {lot_index: gross}} or None when infeasible.
    groups={}
    for i,leaf in enumerate(leaves):groups.setdefault(leaf['ingredient'],[]).append(i)
    draws=[{}for _ in leaves]
    for ingredient,leaf_ids in groups.items():
        lot_ids=[j for j,lot in enumerate(lots) if lot['ingredient']==ingredient]
        sub=_allocate_group([leaves[i] for i in leaf_ids],[lots[j] for j in lot_ids],use_on)
        if sub is None:return None
        for a,i in enumerate(leaf_ids):
            draws[i]={lot_ids[b]:gross for b,gross in sub[a].items()}
    return draws

def _allocate_group(leaves,lots,use_on):
    # Single-ingredient group. All quantities are scaled to integers by
    # their common denominator so the flow arithmetic stays exact and fast.
    order=sorted(range(len(lots)),key=lambda j:(lots[j]['expires_on'],lots[j]['lot_id']))
    demands=[leaf['gross'] for leaf in leaves]
    total=sum(demands,Fraction(0))
    denominators=[q.denominator for q in demands]+[lot['gross_remaining'].denominator for lot in lots]
    scale=math.lcm(*denominators) if denominators else 1
    demand=[int(q*scale) for q in demands]
    balance=[int(lot['gross_remaining']*scale) for lot in lots]
    whole=sum(demand)
    def usable(j,i):
        lot=lots[j];leaf=leaves[i]
        return (lot['expires_on']>=use_on and balance[j]>0 and not (set(lot['tags'])&leaf['forbidden']))
    edge_index={}
    for p,j in enumerate(order):
        for i in range(len(leaves)):
            if usable(j,i):edge_index.setdefault(p,[]).append(i)
    # Phase 1: add lots in priority order and resume the same max-flow; each
    # lot's marginal increase is its maximal total given the higher-priority
    # totals already fixed (later augmentation never reduces them).
    sink=1+len(order)+len(leaves)
    prefix=Dinic(sink+1,zero=0)
    for i,g in enumerate(demand):
        if g>0:prefix.add_edge(1+len(order)+i,sink,g)
    pushed=0;totals=[]
    for p,j in enumerate(order):
        for i in edge_index.get(p,[]):prefix.add_edge(1+p,1+len(order)+i,whole)
        prefix.add_edge(0,1+p,balance[j])
        inc=prefix.max_flow(0,sink)
        totals.append(inc);pushed+=inc
    if pushed<whole:return None
    # Phase 2: fix leaves in order; for each usable lot in priority order
    # take the maximal draw that keeps the remaining instance feasible.
    residual_lot=list(totals)
    residual_leaf=list(demand)
    remaining=whole
    draws=[{}for _ in leaves]
    for i in range(len(leaves)):
        for p,j in enumerate(order):
            if residual_leaf[i]==0:break
            if residual_lot[p]==0 or not usable(j,i):continue
            others=_maxflow(
                [residual_lot[q] for q in range(len(order)) if q!=p],
                [residual_leaf[k] for k in range(len(leaves)) if k!=i],
                [(q if q<p else q-1,k if k<i else k-1)
                 for q in range(len(order)) if q!=p
                 for k in range(len(leaves)) if k!=i
                 if residual_lot[q]>0 and residual_leaf[k]>0 and usable(order[q],k)])
            supplied=others.max_flow(0,1+(len(order)-1)+(len(leaves)-1))
            need=remaining-residual_leaf[i]-supplied
            if need<0:need=0
            take=min(residual_leaf[i],residual_lot[p]-need)
            if take>0:
                draws[i][j]=Fraction(take,scale)
                residual_lot[p]-=take;residual_leaf[i]-=take;remaining-=take
        if residual_leaf[i]!=0:raise ValueError('insufficient stock')
    return draws

def _receipt(request_id,use_on,leaves,lots,draws):
    lines=[]
    grand=Fraction(0)
    for i,leaf in enumerate(leaves):
        items=[]
        cost=Fraction(0);tags=set()
        for j,gross in sorted(draws[i].items(),key=lambda kv:(lots[kv[0]]['expires_on'],lots[kv[0]]['lot_id'])):
            c=gross*lots[j]['price'];cost+=c;tags.update(lots[j]['tags'])
            items.append({'lot_id':lots[j]['lot_id'],'gross':str(gross),'cost':str(c)})
        grand+=cost
        lines.append({'order_id':leaf['order_id'],'path':leaf['path'],'ingredient':leaf['ingredient'],
                      'net':str(leaf['net']),'gross':str(leaf['gross']),'trim':str(leaf['gross']-leaf['net']),
                      'cost':str(cost),'tags':sorted(tags),'draws':items})
    return {'request_id':request_id,'use_on':use_on,'total_cost':str(grand),'lines':lines}

def _crashpoint(name):
    # Internal test hook (not a user-facing option): terminates the process
    # at a reproducible point so recovery behaviour can be exercised.
    if os.environ.get('RECIPE_YIELD_CRASHPOINT')==name:os._exit(99)

def issue(ledger_path,source_path):
    raw=_load_json(source_path,preserve_numbers=True)
    request_fp=fingerprint(raw)
    ingredients,recipes,orders,request_id,use_on,restrictions=_parse_issue(_unwrap(raw))
    leaves=_expand_leaves(ingredients,recipes,orders)
    _bind_restrictions(leaves,orders,restrictions)
    led=_open_ledger(ledger_path)
    with _Lock(led,exclusive=True):
        state=_read_state(led)
        committed=state['requests'].get(request_id)
        if committed is not None:
            if committed['fingerprint']!=request_fp:
                raise ValueError(f'request_id conflict: {request_id} committed with different content')
            return committed['receipt']
        lots=state['lots']
        draws=_allocate(leaves,lots,use_on)
        if draws is None:
            raise ValueError('insufficient stock for full issue')
        receipt=_receipt(request_id,use_on,leaves,lots,draws)
        text=json.dumps(receipt,ensure_ascii=False,sort_keys=True,allow_nan=False)
        for leaf_draws in draws:
            for j,gross in leaf_draws.items():
                lots[j]['gross_remaining']-=gross
        state['requests'][request_id]={'fingerprint':request_fp,'receipt':text}
        _crashpoint('before_commit')
        _write_state(led,state)
        _crashpoint('after_commit')
        return text
