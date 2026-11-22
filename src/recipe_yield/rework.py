"""Offline rework reconciliation.

``rework-reconcile`` rebuilds the physical flow of a rework archive from issue
receipts and production events.  It answers an internal operations question:
given what was known at one registration point and what is known later, which
parcels should be held, which shipped parcels need tracing and which orders
need replenishment.  It never makes a food-safety or compliance judgement.

All arithmetic is exact (``fractions.Fraction``); output quantities are reduced
integers or ``n/d`` strings.
"""
from fractions import Fraction

from .checks import fields, array, ident, integer, number, date, unique, rational

MAX_RECEIPTS = 100
MAX_LINES = 200
MAX_DRAWS = 200
MAX_ORDERS = 100
MAX_LOTS = 30
MAX_EVENTS = 5000
MAX_INPUTS = 30
MAX_AMENDS = 25


# ---------------------------------------------------------------------------
# Strict input parsing
# ---------------------------------------------------------------------------

def _positive(value, label):
    quantity = number(value)
    if quantity <= 0:
        raise ValueError(f'{label} must be positive')
    return quantity


def _non_negative(value, label):
    quantity = number(value)
    if quantity < 0:
        raise ValueError(f'{label} must be non-negative')
    return quantity


def _receipt_positive(value, label):
    # Receipt quantities are the exact strings written by stock-issue, which
    # may be fractions such as "8/3"; they are kept exact, never decimalised.
    quantity = rational(value)
    if quantity <= 0:
        raise ValueError(f'{label} must be positive')
    return quantity


def _receipt_non_negative(value, label):
    quantity = rational(value)
    if quantity < 0:
        raise ValueError(f'{label} must be non-negative')
    return quantity


def _parse_receipt(item):
    # A native stock-issue receipt: exactly the fields and types emitted by
    # ledger._receipt.  The archive is the authority; no batch balance table
    # is invented here.
    fields(item, ('request_id', 'use_on', 'total_cost', 'lines'))
    request_id = ident(item['request_id'])
    date(item['use_on'])
    _receipt_non_negative(item['total_cost'], 'receipt total_cost')
    lines = []
    leaf_ids = set()
    for line in array(item['lines'], 1, MAX_LINES):
        fields(line, ('order_id', 'path', 'ingredient', 'net', 'gross', 'trim',
                      'cost', 'tags', 'draws'))
        order_id = ident(line['order_id'])
        path = array(line['path'], 1, 100)
        for element in path:
            if not isinstance(element, str):
                raise ValueError('invalid receipt path')
        ingredient = ident(line['ingredient'])
        net = _receipt_positive(line['net'], 'receipt net')
        gross = _receipt_positive(line['gross'], 'receipt gross')
        _receipt_non_negative(line['trim'], 'receipt trim')
        _receipt_non_negative(line['cost'], 'receipt cost')
        tags = [ident(t) for t in array(line['tags'], 0, 30)]
        unique(tags)
        leaf = (order_id, tuple(path))
        if leaf in leaf_ids:
            raise ValueError(f'duplicate receipt leaf identity '
                             f'{order_id}/{"/".join(path)}')
        leaf_ids.add(leaf)
        draws = []
        draw_lots = set()
        for draw in array(line['draws'], 1, MAX_DRAWS):
            fields(draw, ('lot_id', 'gross', 'cost'))
            lot_id = ident(draw['lot_id'])
            if lot_id in draw_lots:
                raise ValueError(f'duplicate lot {lot_id} in receipt leaf')
            draw_lots.add(lot_id)
            draw_gross = _receipt_positive(draw['gross'], 'draw gross')
            _receipt_non_negative(draw['cost'], 'draw cost')
            draws.append({'lot_id': lot_id, 'gross': draw_gross})
        if sum((d['gross'] for d in draws), Fraction(0)) != gross:
            raise ValueError('draw gross total does not match line gross')
        lines.append({'order_id': order_id, 'path': tuple(path),
                      'ingredient': ingredient, 'net': net, 'gross': gross,
                      'draws': draws})
    return {'request_id': request_id, 'lines': lines}


def _parse_receipts(items):
    receipts = [_parse_receipt(item) for item in array(items, 1, MAX_RECEIPTS)]
    unique([receipt['request_id'] for receipt in receipts])
    quotas = {}
    for receipt in receipts:
        rid = receipt['request_id']
        for line in receipt['lines']:
            for draw in line['draws']:
                key = (rid, draw['lot_id'])
                quotas[key] = quotas.get(key, Fraction(0)) + draw['gross']
    return receipts, quotas


def _parse_inputs(items):
    seen = set()
    parsed = []
    for item in array(items, 1, MAX_INPUTS):
        if not isinstance(item, dict):
            raise ValueError('mix input must be an object')
        if 'request_id' in item:
            fields(item, ('request_id', 'lot_id', 'grams'))
            key = ('receipt', ident(item['request_id']), ident(item['lot_id']))
            source = {'request_id': key[1], 'lot_id': key[2]}
        else:
            fields(item, ('mix_id', 'grams'))
            key = ('mix', ident(item['mix_id']))
            source = {'mix_id': key[1]}
        if key in seen:
            raise ValueError(f'duplicate input source in mix: {key[-1]}')
        seen.add(key)
        parsed.append((source, _positive(item['grams'], 'input grams')))
    return parsed


def _parse_event(item, position):
    if not isinstance(item, dict):
        raise ValueError('event must be an object')
    kind = item.get('kind')
    if kind == 'mix':
        fields(item, ('id', 'kind', 'inputs', 'output_grams'))
        inputs = _parse_inputs(item['inputs'])
        output = _non_negative(item['output_grams'], 'mix output_grams')
        total = sum((grams for _, grams in inputs), Fraction(0))
        if output > total:
            raise ValueError(f'mix {item["id"]} output exceeds input mass')
        return {'pos': position, 'kind': 'mix', 'id': ident(item['id']),
                'inputs': inputs, 'output': output}
    if kind == 'pack':
        fields(item, ('id', 'kind', 'mix_id', 'order_id', 'grams'))
        return {'pos': position, 'kind': 'pack', 'id': ident(item['id']),
                'mix_id': ident(item['mix_id']),
                'order_id': ident(item['order_id']),
                'grams': _positive(item['grams'], 'pack grams')}
    if kind == 'ship':
        fields(item, ('id', 'kind', 'parcel_id'))
        return {'pos': position, 'kind': 'ship', 'id': ident(item['id']),
                'parcel_id': ident(item['parcel_id'])}
    if kind == 'amend':
        fields(item, ('id', 'kind', 'mix_id', 'inputs'))
        return {'pos': position, 'kind': 'amend', 'id': ident(item['id']),
                'mix_id': ident(item['mix_id']),
                'inputs': _parse_inputs(item['inputs'])}
    raise ValueError('invalid event kind')


def _parse_orders(items):
    orders = {}
    for item in array(items, 1, MAX_ORDERS):
        fields(item, ('id', 'demand_grams'))
        key = ident(item['id'])
        if key in orders:
            raise ValueError(f'duplicate order id: {key}')
        orders[key] = _positive(item['demand_grams'], 'demand_grams')
    return orders


def parse(data):
    fields(data, ('receipts', 'orders', 'events', 'lot_ids', 'compare_to',
                  'as_known'))
    receipts, quotas = _parse_receipts(data['receipts'])
    orders = _parse_orders(data['orders'])
    lot_ids = [ident(value) for value in array(data['lot_ids'], 1, MAX_LOTS)]
    unique(lot_ids)
    present = {draw['lot_id'] for receipt in receipts
               for line in receipt['lines'] for draw in line['draws']}
    for lot_id in lot_ids:
        if lot_id not in present:
            raise ValueError(f'lot_id {lot_id} does not appear in any receipt')
    known = len(array(data['events'], 0, MAX_EVENTS))
    compare_to = integer(data['compare_to'], 0, known)
    as_known = integer(data['as_known'], 0, known)
    if compare_to > as_known:
        raise ValueError('compare_to may not exceed as_known')
    # Records beyond as_known are invisible to this reconciliation: their
    # shape, fields and references are not this report's concern.
    events = [_parse_event(item, position)
              for position, item in enumerate(data['events'][:as_known], 1)]
    return {'receipts': receipts, 'quotas': quotas, 'orders': orders,
            'events': events, 'lot_ids': lot_ids, 'compare_to': compare_to,
            'as_known': as_known}


# ---------------------------------------------------------------------------
# Registration grammar of the visible prefix
# ---------------------------------------------------------------------------

def _mixes(visible):
    mixes = {}
    for event in visible:
        if event['kind'] == 'mix':
            if event['id'] in mixes:
                raise ValueError(f'duplicate mix id: {event["id"]}')
            mixes[event['id']] = event
    return mixes


def _validate(visible, mixes, orders):
    seen = set()
    for event in visible:
        if event['id'] in seen:
            raise ValueError(f'duplicate event id: {event["id"]}')
        seen.add(event['id'])
    amendments = 0
    for event in visible:
        kind = event['kind']
        if kind == 'pack':
            target = mixes.get(event['mix_id'])
            if target is None:
                raise ValueError(f'pack {event["id"]} references unknown mix '
                                 f'{event["mix_id"]}')
            if target['pos'] > event['pos']:
                raise ValueError(f'pack {event["id"]} references a mix '
                                 f'registered later')
            if event['order_id'] not in orders:
                raise ValueError(f'pack {event["id"]} references unknown '
                                 f'order {event["order_id"]}')
        elif kind == 'amend':
            amendments += 1
            amended = mixes.get(event['mix_id'])
            if amended is None:
                raise ValueError(f'amend references unknown mix '
                                 f'{event["mix_id"]}')
            if amended['pos'] >= event['pos']:
                raise ValueError('amend precedes the mix it corrects')
            _check_sources(event['inputs'], mixes, amended)
            if _total(event['inputs']) != _total(amended['inputs']):
                raise ValueError('amend must keep the mix input total weight')
        elif kind == 'mix':
            _check_sources(event['inputs'], mixes, event)
    if amendments > MAX_AMENDS:
        raise ValueError('too many amendments')


def _total(inputs):
    return sum((grams for _, grams in inputs), Fraction(0))


def _check_sources(inputs, mixes, mix_event):
    # A sub-batch source must predate the mix event it feeds, so neither an
    # original mix nor an amendment can point at a batch that only exists at
    # or after that position.
    for source, _ in inputs:
        if 'request_id' in source:
            continue
        other = mixes.get(source['mix_id'])
        if other is None:
            raise ValueError(f'mix source {source["mix_id"]} is unknown')
        if other['pos'] >= mix_event['pos']:
            raise ValueError(f'mix source {source["mix_id"]} is not earlier '
                             f'than the mix event')


# ---------------------------------------------------------------------------
# Flow feasibility over every visible prefix
# ---------------------------------------------------------------------------

def _apply(inputs, used, available, quotas, mix_id):
    for source, grams in inputs:
        if 'request_id' in source:
            key = (source['request_id'], source['lot_id'])
            quota = quotas.get(key)
            if quota is None:
                raise ValueError(f'mix {mix_id} draws from a receipt source '
                                 f'absent from the archive: {key[0]}/{key[1]}')
            amount = used.get(key, Fraction(0)) + grams
            if amount > quota:
                raise ValueError(f'receipt credit exceeded for '
                                 f'{key[0]}/{key[1]}')
            used[key] = amount
        else:
            other = source['mix_id']
            stock = available.get(other, Fraction(0))
            if grams > stock:
                raise ValueError(f'mix {mix_id} overdraws mix {other}')
            available[other] = stock - grams


def _release(inputs, used, available):
    for source, grams in inputs:
        if 'request_id' in source:
            key = (source['request_id'], source['lot_id'])
            used[key] -= grams
        else:
            other = source['mix_id']
            available[other] = available.get(other, Fraction(0)) + grams


def _check_flow(visible, quotas):
    """Reject the first visible prefix at which the physical flow is unsound.

    Mix and pack events only consume; shipments only mark parcels.  An
    amendment revokes the old group and applies the new one at its own
    position, so every intermediate prefix is examined exactly once.
    """
    used = {}
    available = {}
    groups = {}
    packs = set()
    shipped = set()
    for event in visible:
        kind = event['kind']
        if kind == 'mix':
            _apply(event['inputs'], used, available, quotas, event['id'])
            available[event['id']] = (available.get(event['id'], Fraction(0))
                                      + event['output'])
            groups[event['id']] = event['inputs']
        elif kind == 'amend':
            _release(groups[event['mix_id']], used, available)
            _apply(event['inputs'], used, available, quotas, event['mix_id'])
            groups[event['mix_id']] = event['inputs']
        elif kind == 'pack':
            stock = available.get(event['mix_id'], Fraction(0))
            if event['grams'] > stock:
                raise ValueError(f'pack {event["id"]} overdraws mix '
                                 f'{event["mix_id"]}')
            available[event['mix_id']] = stock - event['grams']
            if event['id'] in packs:
                raise ValueError(f'duplicate parcel id: {event["id"]}')
            packs.add(event['id'])
        else:
            parcel_id = event['parcel_id']
            if parcel_id not in packs:
                raise ValueError(f'ship {event["id"]} references an unknown '
                                 f'parcel: {parcel_id}')
            if parcel_id in shipped:
                raise ValueError(f'parcel shipped twice: {parcel_id}')
            shipped.add(parcel_id)


# ---------------------------------------------------------------------------
# Attribution snapshot at one cutoff
# ---------------------------------------------------------------------------

def _effective(visible, cutoff):
    groups = {}
    for event in visible:
        if event['pos'] > cutoff:
            break
        if event['kind'] == 'mix':
            groups[event['id']] = event['inputs']
        elif event['kind'] == 'amend':
            groups[event['mix_id']] = event['inputs']
    return groups


def _snapshot(visible, cutoff, watched):
    groups = _effective(visible, cutoff)
    produced = {}
    attributable = {}
    available = {}
    packs = {}
    shipped = set()
    for event in visible:
        if event['pos'] > cutoff:
            break
        kind = event['kind']
        if kind == 'mix':
            mix_id = event['id']
            inputs = groups[mix_id]
            attributed = Fraction(0)
            for source, grams in inputs:
                if 'request_id' in source:
                    if source['lot_id'] in watched:
                        attributed += grams
                else:
                    other = source['mix_id']
                    available[other] -= grams
                    if attributable[other]:
                        attributed += (grams * attributable[other]
                                       / produced[other])
            total = sum((grams for _, grams in inputs), Fraction(0))
            output = event['output']
            produced[mix_id] = output
            attributable[mix_id] = (output * attributed / total
                                    if total else Fraction(0))
            available[mix_id] = available.get(mix_id, Fraction(0)) + output
        elif kind == 'pack':
            mix_id = event['mix_id']
            available[mix_id] -= event['grams']
            if attributable.get(mix_id):
                share = event['grams'] * attributable[mix_id] / produced[mix_id]
            else:
                share = Fraction(0)
            packs[event['id']] = {'order_id': event['order_id'],
                                  'grams': event['grams'],
                                  'attributed': share}
        elif kind == 'ship':
            shipped.add(event['parcel_id'])
    return packs, shipped


def _summarise(packs, shipped, orders):
    parcels = []
    for parcel_id in sorted(packs):
        pack = packs[parcel_id]
        if pack['attributed'] > 0:
            action = 'trace' if parcel_id in shipped else 'hold'
        else:
            action = 'shipped_clear' if parcel_id in shipped else 'ready'
        parcels.append({'id': parcel_id, 'order_id': pack['order_id'],
                        'grams': str(pack['grams']),
                        'attributed_grams': str(pack['attributed']),
                        'action': action})
    buckets = {order_id: {'ready': Fraction(0),
                          'shipped_clear': Fraction(0),
                          'hold': Fraction(0),
                          'trace': Fraction(0)} for order_id in orders}
    for row in parcels:
        bucket = buckets[row['order_id']]
        bucket[row['action']] += Fraction(row['grams'])
    order_rows = []
    for order_id in sorted(orders):
        bucket = buckets[order_id]
        gap = orders[order_id] - bucket['shipped_clear'] - bucket['ready']
        if gap < 0:
            gap = Fraction(0)
        order_rows.append({'id': order_id,
                           'demand_grams': str(orders[order_id]),
                           'ready': str(bucket['ready']),
                           'shipped_clear': str(bucket['shipped_clear']),
                           'hold': str(bucket['hold']),
                           'trace': str(bucket['trace']),
                           'gap_grams': str(gap)})
    return {'parcels': parcels, 'orders': order_rows}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def reconcile(data):
    parsed = parse(data)
    visible = parsed['events']
    orders = parsed['orders']
    mixes = _mixes(visible)
    _validate(visible, mixes, orders)
    _check_flow(visible, parsed['quotas'])
    watched = set(parsed['lot_ids'])
    compare_to = parsed['compare_to']
    as_known = parsed['as_known']

    before = _summarise(*_snapshot(visible, compare_to, watched), orders)
    after = _summarise(*_snapshot(visible, as_known, watched), orders)
    before_actions = {row['id']: row['action'] for row in before['parcels']}
    changes = []
    for row in after['parcels']:
        previous = before_actions.get(row['id'])
        if previous != row['action']:
            changes.append({'id': row['id'], 'before': previous,
                            'after': row['action']})
    changes.sort(key=lambda change: change['id'])
    return {'before': before, 'after': after, 'action_changes': changes}
