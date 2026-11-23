"""Offline settlement books for confirmed supplier payments.

A settlement book owns one fixed lot table and one coding scheme of
processing orders.  ``settle-close`` archives original ``stock-issue``
receipts and, per period, allocates the cumulative confirmed payment of
each lot across the processing orders that actually drew from it and the
remaining unclaimed stock.  Vouchers carry only the period-to-period
delta of the cumulative target allocation; sealed vouchers are never
rewritten.

All weights stay exact (``fractions.Fraction``); cents stay integers.
"""
import json
import os
import fcntl
import tempfile
from fractions import Fraction
from pathlib import Path

from .checks import fields, array, ident, unique, number, rational, date
from .io import pairs, constant
from .ledger import _Number, fingerprint, _unwrap

VERSION = 1
MAX_LOTS = 200
MAX_RECEIPTS = 100
MAX_LINES = 200
MAX_DRAWS = 200
MAX_TAGS = 30
MAX_PATH = 100
MAX_CENTS = 10 ** 12


# ---------------------------------------------------------------------------
# Crash injection (test hook, mirrors ledger._crashpoint)
# ---------------------------------------------------------------------------

def _crashpoint(name):
    if os.environ.get('RECIPE_YIELD_CRASHPOINT') == name:
        os._exit(99)


# ---------------------------------------------------------------------------
# Strict JSON loading and book state persistence
# ---------------------------------------------------------------------------

def _load_json(path, preserve_numbers=False):
    with open(path, 'rb') as f:
        raw = f.read(8000001)
    if len(raw) > 8000000:
        raise ValueError('input over 8MB')
    text = raw.decode('utf-8')
    if preserve_numbers:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=constant, parse_int=_Number,
                          parse_float=_Number)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _state_to_json(state):
    return {'version': VERSION,
            'lots': [{'lot_id': lot['lot_id'],
                      'ingredient': lot['ingredient'],
                      'received_grams': str(lot['received'])}
                     for lot in state['lots']],
            'receipts': state['receipts'],
            'periods': state['periods']}


def _state_from_json(data):
    if not isinstance(data, dict):
        raise ValueError('unsupported book state')
    fields(data, ('version', 'lots', 'receipts', 'periods'))
    if data['version'] != VERSION:
        raise ValueError('unsupported book version')
    lots = []
    for item in array(data['lots'], 1, MAX_LOTS):
        fields(item, ('lot_id', 'ingredient', 'received_grams'))
        received = rational(item['received_grams'])
        if received <= 0:
            raise ValueError('negative lot weight in book')
        lots.append({'lot_id': ident(item['lot_id']),
                     'ingredient': ident(item['ingredient']),
                     'received': received})
    unique([lot['lot_id'] for lot in lots])
    receipts = data['receipts']
    if not isinstance(receipts, dict):
        raise ValueError('invalid book receipt archive')
    for key, value in receipts.items():
        ident(key)
        if not isinstance(value, dict):
            raise ValueError('invalid archived receipt')
        fields(value, ('period', 'receipt'))
        ident(value['period'])
        if not isinstance(value['receipt'], dict):
            raise ValueError('invalid archived receipt payload')
    periods = data['periods']
    if not isinstance(periods, list):
        raise ValueError('invalid book period chain')
    seen_periods = set()
    last = None
    for record in periods:
        fields(record, ('period', 'previous', 'fingerprint',
                        'confirmed', 'voucher'))
        period = ident(record['period'])
        if period in seen_periods:
            raise ValueError('duplicate period in book')
        seen_periods.add(period)
        previous = record['previous']
        if previous is not None:
            ident(previous)
            if last != previous:
                raise ValueError('broken period chain in book')
        elif last is not None:
            raise ValueError('null previous in non-empty book')
        last = period
        if not isinstance(record['fingerprint'], str) \
                or not isinstance(record['voucher'], str):
            raise ValueError('invalid sealed period record')
        confirmed = record['confirmed']
        if not isinstance(confirmed, dict) \
                or set(confirmed) != {lot['lot_id'] for lot in lots}:
            raise ValueError('invalid sealed confirmed table')
        for lot_id, cents in confirmed.items():
            ident(lot_id)
            if type(cents) is not int or not 0 <= cents <= MAX_CENTS:
                raise ValueError('invalid sealed confirmed cents')
    return {'lots': lots, 'receipts': receipts, 'periods': periods}


def _read_state(book):
    state_path = book / 'book.json'
    if not state_path.is_file():
        raise ValueError('book state missing or unreadable')
    try:
        data = json.loads(state_path.read_bytes().decode('utf-8'),
                          object_pairs_hook=pairs, parse_constant=constant)
        return _state_from_json(data)
    except (ValueError, UnicodeError) as e:
        raise ValueError(f'cannot safely read book: {e}') from e


def _write_state(book, state):
    payload = (json.dumps(_state_to_json(state), ensure_ascii=False,
                          sort_keys=True, separators=(',', ':'),
                          allow_nan=False) + '\n').encode('utf-8')
    for stale in book.glob('.book-*.tmp'):
        stale.unlink(missing_ok=True)
    temporary = book / f'.book-{os.getpid()}.tmp'
    with open(temporary, 'wb') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, book / 'book.json')
    dirfd = os.open(book, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


class _Lock:
    def __init__(self, book, exclusive):
        self.book = book
        self.exclusive = exclusive
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.book / 'lock', os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(self.fd, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None
        return False


def _open_book(path):
    book = Path(path)
    if not book.is_dir():
        raise ValueError('book not found')
    return book


# ---------------------------------------------------------------------------
# settle-init
# ---------------------------------------------------------------------------

def parse_book_lots(data):
    fields(data, ('lots',))
    lots = []
    for item in array(data['lots'], 1, MAX_LOTS):
        fields(item, ('lot_id', 'ingredient', 'received_grams'))
        lot_id = ident(item['lot_id'])
        ingredient = ident(item['ingredient'])
        received = number(item['received_grams'])
        if received <= 0:
            raise ValueError('received_grams must be positive')
        lots.append({'lot_id': lot_id, 'ingredient': ingredient,
                     'received': received})
    unique([lot['lot_id'] for lot in lots])
    return lots


def init_book(book_path, source_path):
    data = _load_json(source_path)
    lots = parse_book_lots(data)
    book = Path(book_path)
    try:
        os.mkdir(book)
    except (FileExistsError, NotADirectoryError):
        raise ValueError('book path already exists') from None
    try:
        _write_state(book, {'lots': lots, 'receipts': {}, 'periods': []})
    except (ValueError, OSError):
        # Best effort: never leave a half-created book behind.
        for stale in book.glob('.book-*.tmp'):
            stale.unlink(missing_ok=True)
        (book / 'book.json').unlink(missing_ok=True)
        try:
            os.rmdir(book)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Receipt validation (original stock-issue receipts, archive is authority)
# ---------------------------------------------------------------------------

def _positive_rational(value, label):
    quantity = rational(value)
    if quantity <= 0:
        raise ValueError(f'{label} must be positive')
    return quantity


def _non_negative_rational(value, label):
    quantity = rational(value)
    if quantity < 0:
        raise ValueError(f'{label} must be non-negative')
    return quantity


def _parse_receipt(item, lot_map, leaf_seen):
    fields(item, ('request_id', 'use_on', 'total_cost', 'lines'))
    request_id = ident(item['request_id'])
    date(item['use_on'])
    _non_negative_rational(item['total_cost'], 'receipt total_cost')
    parsed_lines = []
    for line in array(item['lines'], 1, MAX_LINES):
        fields(line, ('order_id', 'path', 'ingredient', 'net', 'gross',
                      'trim', 'cost', 'tags', 'draws'))
        order_id = ident(line['order_id'])
        path = array(line['path'], 1, MAX_PATH)
        for element in path:
            if not isinstance(element, str):
                raise ValueError('invalid receipt path')
        ingredient = ident(line['ingredient'])
        _positive_rational(line['net'], 'receipt net')
        gross = _positive_rational(line['gross'], 'receipt gross')
        _non_negative_rational(line['trim'], 'receipt trim')
        _non_negative_rational(line['cost'], 'receipt cost')
        tags = [ident(tag) for tag in array(line['tags'], 0, MAX_TAGS)]
        unique(tags)
        leaf = (order_id, tuple(path))
        if leaf in leaf_seen:
            raise ValueError(
                f'duplicate receipt leaf identity {order_id}/'
                f'{"".join(path)}')
        leaf_seen.add(leaf)
        draw_lots = set()
        draw_sum = Fraction(0)
        parsed_draws = []
        for draw in array(line['draws'], 1, MAX_DRAWS):
            fields(draw, ('lot_id', 'gross', 'cost'))
            lot_id = ident(draw['lot_id'])
            lot = lot_map.get(lot_id)
            if lot is None:
                raise ValueError(f'unknown lot in receipt draw: {lot_id}')
            if lot['ingredient'] != ingredient:
                raise ValueError(
                    f'lot {lot_id} ingredient does not match leaf ingredient')
            if lot_id in draw_lots:
                raise ValueError(f'duplicate lot {lot_id} in receipt leaf')
            draw_lots.add(lot_id)
            draw_gross = _positive_rational(draw['gross'], 'draw gross')
            _non_negative_rational(draw['cost'], 'draw cost')
            draw_sum += draw_gross
            parsed_draws.append({'lot_id': lot_id, 'gross': draw['gross']})
        if draw_sum != gross:
            raise ValueError('draw gross total does not match line gross')
        parsed_lines.append({'order_id': order_id, 'path': list(path),
                             'draws': parsed_draws})
    return request_id, parsed_lines


# ---------------------------------------------------------------------------
# Exact largest-remainder allocation per lot
# ---------------------------------------------------------------------------

def _order_weights(lots, receipt_payloads):
    weights = {lot['lot_id']: {} for lot in lots}
    for payload in receipt_payloads:
        for line in payload['lines']:
            order_id = line['order_id']
            for draw in line['draws']:
                bucket = weights[draw['lot_id']]
                bucket[order_id] = bucket.get(order_id, Fraction(0)) \
                    + rational(draw['gross'])
    return weights


def _allocate_lot(lot, order_weights, cents):
    """Largest-remainder split of cumulative confirmed cents.

    Buckets are the drawing orders and the unclaimed stock bucket
    (order_id ``None``).  Zero-weight buckets never participate; each lot
    is allocated independently with exact Fraction arithmetic.

    Floors are taken from each bucket's exact share, then the remaining
    whole cents are handed out one each by descending fractional
    remainder.  On equal remainders, order buckets precede the stock
    bucket and order among themselves by ASCII order_id; the stock bucket
    always sorts behind every tied order bucket.
    """
    received = lot['received']
    buckets = []  # (bucket id, exact weight)
    used = Fraction(0)
    for order_id, weight in order_weights.items():
        if weight > 0:
            buckets.append((order_id, weight))
            used += weight
    leftover = received - used
    if leftover > 0:
        buckets.append((None, leftover))
    targets = {}
    ranked = []
    allocated = 0
    for bucket_id, weight in buckets:
        share = Fraction(cents) * weight / received
        floor = share.numerator // share.denominator
        remainder = share - floor
        targets[bucket_id] = floor
        allocated += floor
        # class 0 = order (tie broken by ASCII id), class 1 = stock.
        class_key = (0, bucket_id) if bucket_id is not None else (1, '')
        ranked.append((remainder, class_key, bucket_id))
    extra = cents - allocated
    if extra < 0 or extra > len(buckets):
        raise ValueError('payment allocation failed')
    # Largest remainder first; Fraction stays exact, never float-rounded.
    ranked.sort(key=lambda row: (-row[0], row[1]))
    for _, _, bucket_id in ranked[:extra]:
        targets[bucket_id] += 1
    return targets


def _cumulative_targets(lots, weights, confirmed):
    result = {}
    for lot in lots:
        result[lot['lot_id']] = _allocate_lot(
            lot, weights.get(lot['lot_id'], {}), confirmed[lot['lot_id']])
    return result


# ---------------------------------------------------------------------------
# settle-close
# ---------------------------------------------------------------------------

def _parse_confirmed(items, lots):
    expected = [lot['lot_id'] for lot in lots]
    array(items, len(expected), len(expected))
    confirmed = {}
    for item in items:
        fields(item, ('lot_id', 'cents'))
        lot_id = ident(item['lot_id'])
        if lot_id not in expected:
            raise ValueError(f'confirmed entry for unknown lot: {lot_id}')
        if lot_id in confirmed:
            raise ValueError(f'duplicate confirmed entry for lot: {lot_id}')
        cents = item['cents']
        if type(cents) is not int or not 0 <= cents <= MAX_CENTS:
            raise ValueError(f'cents must be an integer in 0..{MAX_CENTS}')
        confirmed[lot_id] = cents
    if set(confirmed) != set(expected):
        raise ValueError('confirmed table must cover every lot exactly once')
    return confirmed


def _voucher_bytes(period, previous, entries):
    voucher = {'period': period, 'previous': previous, 'entries': entries}
    return (json.dumps(voucher, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)
            + '\n').encode('utf-8')


def _build_entries(lots, current_targets, prior_targets,
                   current_confirmed, prior_confirmed):
    entries = []
    for lot in sorted(lots, key=lambda item: item['lot_id']):
        lot_id = lot['lot_id']
        current = current_targets[lot_id]
        prior = prior_targets.get(lot_id, {})
        delta_sum = 0
        keys = set(current) | set(prior)
        ordered = sorted(keys, key=lambda bucket:
                         (0, bucket) if bucket is not None else (1, ''))
        for bucket in ordered:
            delta = current.get(bucket, 0) - prior.get(bucket, 0)
            delta_sum += delta
            if delta != 0:
                entries.append({'lot_id': lot_id, 'order_id': bucket,
                                'delta_cents': delta})
        if delta_sum != current_confirmed[lot_id] - prior_confirmed[lot_id]:
            raise ValueError('voucher delta total does not match payment change')
    return entries


def close_period(book_path, source_path):
    raw = _load_json(source_path, preserve_numbers=True)
    request_fingerprint = fingerprint(raw)
    data = _unwrap(raw)
    fields(data, ('period', 'previous', 'receipts', 'confirmed'))
    period = ident(data['period'])
    previous = data['previous']
    if previous is not None:
        previous = ident(previous)
    receipt_items = array(data['receipts'], 0, MAX_RECEIPTS)

    book = _open_book(book_path)
    with _Lock(book, exclusive=True):
        state = _read_state(book)
        lots = state['lots']
        lot_map = {lot['lot_id']: lot for lot in lots}

        existing = next((record for record in state['periods']
                         if record['period'] == period), None)
        if existing is not None:
            # Idempotent retry: hand the original result back without
            # touching receipts or requiring the previous period to stay last.
            if existing['fingerprint'] != request_fingerprint:
                raise ValueError(
                    f'period {period} already closed with different content')
            return {'period': existing['period'],
                    'previous': existing['previous']}

        last_period = state['periods'][-1]['period'] if state['periods'] else None
        if last_period is None:
            if previous is not None:
                raise ValueError('previous must be null for an empty book')
        elif previous != last_period:
            raise ValueError(
                f'previous must be the current last period {last_period}')

        confirmed = _parse_confirmed(data['confirmed'], lots)

        new_receipts = []
        new_request_ids = set()
        new_weights = {lot['lot_id']: {} for lot in lots}
        for item in receipt_items:
            # Leaf identity uniqueness is scoped to a single receipt: the
            # same (order_id, path) may legitimately appear on separate
            # issue receipts and merges by order during aggregation.
            request_id, parsed_lines = _parse_receipt(
                item, lot_map, set())
            if request_id in state['receipts']:
                raise ValueError(
                    f'request_id already archived in an earlier period: '
                    f'{request_id}')
            if request_id in new_request_ids:
                raise ValueError(f'duplicate request_id in close request: '
                                 f'{request_id}')
            new_request_ids.add(request_id)
            new_receipts.append((request_id, item))
            for line in parsed_lines:
                for draw in line['draws']:
                    bucket = new_weights[draw['lot_id']]
                    bucket[line['order_id']] = \
                        bucket.get(line['order_id'], Fraction(0)) \
                        + rational(draw['gross'])

        # Cumulative draw per lot may not exceed the purchased gross.
        archived_payloads = [entry['receipt']
                             for entry in state['receipts'].values()]
        archived_weights = _order_weights(lots, archived_payloads)
        for lot in lots:
            lot_id = lot['lot_id']
            total = sum(archived_weights.get(lot_id, {}).values(),
                        Fraction(0))
            total += sum(new_weights.get(lot_id, {}).values(), Fraction(0))
            if total > lot['received']:
                raise ValueError(
                    f'cumulative draws exceed received gross for lot {lot_id}')

        prior_confirmed = (state['periods'][-1]['confirmed']
                           if state['periods']
                           else {lot['lot_id']: 0 for lot in lots})
        prior_targets = _cumulative_targets(lots, archived_weights,
                                            prior_confirmed)

        current_payloads = archived_payloads + [item for _, item in new_receipts]
        current_weights = _order_weights(lots, current_payloads)
        current_targets = _cumulative_targets(lots, current_weights, confirmed)

        entries = _build_entries(lots, current_targets, prior_targets,
                                 confirmed, prior_confirmed)
        voucher_text = _voucher_bytes(period, previous, entries).decode('utf-8')

        for request_id, item in new_receipts:
            state['receipts'][request_id] = {'period': period,
                                             'receipt': item}
        state['periods'].append(
            {'period': period, 'previous': previous,
             'fingerprint': request_fingerprint,
             'confirmed': confirmed, 'voucher': voucher_text})

        _crashpoint('settle_before_commit')
        _write_state(book, state)
        _crashpoint('settle_after_commit')
        return {'period': period, 'previous': previous}


# ---------------------------------------------------------------------------
# settle-export
# ---------------------------------------------------------------------------

def _publish_voucher(target, payload):
    target = Path(target)
    if target.is_symlink():
        raise ValueError('symlink output rejected')
    if target.exists():
        if not target.is_file():
            raise ValueError('output path is not a regular file')
        if target.read_bytes() != payload:
            raise ValueError('existing output differs from sealed voucher')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,
                                         prefix='.voucher-',
                                         delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            # A concurrent export may have placed the same voucher.
            if target.is_symlink() or not target.is_file() \
                    or target.read_bytes() != payload:
                raise ValueError('output path was taken by another file')
            return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    dirfd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    _crashpoint('settle_export_after_publish')


def export_period(book_path, period_id, output_path):
    period = ident(period_id)
    book = _open_book(book_path)
    with _Lock(book, exclusive=False):
        state = _read_state(book)
        record = next((item for item in state['periods']
                       if item['period'] == period), None)
        if record is None:
            raise ValueError(f'period is not closed: {period}')
        payload = record['voucher'].encode('utf-8')
    _publish_voucher(output_path, payload)
