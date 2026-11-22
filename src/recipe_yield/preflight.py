from fractions import Fraction

from .checks import fields, array, ident, unique, number
from . import model

SUPPRESS = 'suppress'
RESTORE = 'restore'
MAX_PATH = 100


def _path_element(value):
    if not isinstance(value, str):
        raise ValueError('invalid declaration path')
    parts = value.rsplit(':', 1)
    if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
        raise ValueError('invalid declaration path')
    return value


def _resolve_path(recipes, root, path):
    # A declaration path follows component indices from the order root and
    # may stop at a sub-recipe component or at an ingredient component.
    current = root
    for position, element in enumerate(path):
        name, index_text = element.rsplit(':', 1)
        if name != current:
            raise ValueError('declaration path does not match recipe component')
        index = int(index_text)
        components = recipes[current]['components']
        if not 0 <= index < len(components):
            raise ValueError('declaration path index out of range')
        kind, ref, _ = components[index]
        if kind == 'ingredient':
            if position != len(path) - 1:
                raise ValueError('declaration path continues past an ingredient component')
            return
        current = ref


def parse_preflight(data):
    ingredients, recipes, orders = model.parse(
        data, extra=('thresholds', 'declarations', 'print_jobs'))
    order_map = {order_id: (ref, grams) for order_id, ref, grams in orders}

    thresholds = {}
    for item in array(data['thresholds'], 1, 30):
        fields(item, ('tag', 'min_share'))
        tag = ident(item['tag'])
        if tag in thresholds:
            raise ValueError('duplicate threshold tag')
        share = number(item['min_share'])
        if not Fraction(0) < share <= 1:
            raise ValueError('min_share must be within (0,1]')
        thresholds[tag] = share

    # declarations[(order_id, tag)] maps a full component path to an action.
    declarations = {}
    triples = set()
    for item in array(data['declarations'], 0, 200):
        fields(item, ('order_id', 'path', 'tag', 'action'))
        order_id = ident(item['order_id'])
        tag = ident(item['tag'])
        if order_id not in order_map:
            raise ValueError('unknown order in declaration')
        if tag not in thresholds:
            raise ValueError('declaration tag has no threshold')
        action = item['action']
        if action not in (SUPPRESS, RESTORE):
            raise ValueError('invalid declaration action')
        path = tuple(_path_element(element) for element in array(item['path'], 1, MAX_PATH))
        _resolve_path(recipes, order_map[order_id][0], path)
        triple = (order_id, path, tag)
        if triple in triples:
            raise ValueError('duplicate declaration')
        triples.add(triple)
        declarations.setdefault((order_id, tag), {})[path] = action

    jobs = []
    job_ids = set()
    covered = {}
    for item in array(data['print_jobs'], 1, 100):
        fields(item, ('id', 'order_ids', 'tags'))
        job_id = ident(item['id'])
        if job_id in job_ids:
            raise ValueError('duplicate print job id')
        job_ids.add(job_id)
        order_ids = [ident(value) for value in array(item['order_ids'], 1, 100)]
        unique(order_ids)
        for order_id in order_ids:
            if order_id not in order_map:
                raise ValueError('print job references unknown order')
            if order_id in covered:
                raise ValueError('order belongs to more than one print job')
            covered[order_id] = job_id
        tags = [ident(value) for value in array(item['tags'], 0, 30)]
        unique(tags)
        jobs.append({'id': job_id, 'order_ids': order_ids, 'tags': tags})
    if set(covered) != set(order_map):
        raise ValueError('every order must be listed in exactly one print job')

    return ingredients, recipes, orders, thresholds, declarations, jobs


def _total_net(recipes, root, grams):
    # Denominator: sum of every ingredient leaf net weight. Declarations never
    # change the denominator, so this memo is independent of tags and scopes.
    memo = {}

    def walk(recipe, amount):
        key = (recipe, amount)
        if key in memo:
            return memo[key]
        scale = amount / recipes[recipe]['output']
        total = Fraction(0)
        for kind, ref, net in recipes[recipe]['components']:
            required = net * scale
            if kind == 'ingredient':
                total += required
            else:
                total += walk(ref, required)
        memo[key] = total
        return total

    return walk(root, grams)


def _tag_stats(ingredients, recipes, root, grams, tag, scopes):
    # Returns (effective net, effective leaf occurrences, first full path).
    # The tree is only expanded explicitly along declaration prefixes; every
    # declaration-free subtree is answered by a memoized walk, so exponentially
    # shared recipes never materialize their leaves.
    # Component paths that carry an action, plus every component path above
    # them. A recipe frame reached through one of these component paths stays
    # explicit so the action on the frame itself and on its descendants is
    # read; every other subtree collapses into the memoized clean_walk while
    # preserving the suppressed state inherited from ancestors.
    explicit_nodes = set()
    for declaration_path in scopes:
        for k in range(1, len(declaration_path) + 1):
            explicit_nodes.add(declaration_path[:k])
    # The root frame is explicit whenever the order carries any declaration,
    # because a declaration on a top-level component otherwise has no explicit
    # ancestor frame to read it.
    if scopes:
        explicit_nodes.add(())
    memo = {}

    def clean_walk(recipe, amount, suppressed):
        key = (recipe, amount, suppressed)
        cached = memo.get(key)
        if cached is not None:
            return cached
        scale = amount / recipes[recipe]['output']
        net_sum = Fraction(0)
        count = 0
        first = None
        for index, (kind, ref, net) in enumerate(recipes[recipe]['components']):
            required = net * scale
            element = f'{recipe}:{index}'
            if kind == 'ingredient':
                if not suppressed and tag in ingredients[ref]['tags']:
                    net_sum += required
                    count += 1
                    if first is None:
                        first = [element]
            else:
                child_net, child_count, child_first = clean_walk(ref, required, suppressed)
                net_sum += child_net
                count += child_count
                if child_first is not None and first is None:
                    first = [element] + child_first
        result = (net_sum, count, first)
        memo[key] = result
        return result

    def walk(recipe, amount, suppressed, prefix):
        if prefix not in explicit_nodes:
            # clean_walk answers relative to this subtree's entry; prefix is
            # the absolute component path that reaches the entry (() at root).
            net_sum, count, first = clean_walk(recipe, amount, suppressed)
            if first is None:
                return net_sum, count, None
            return net_sum, count, list(prefix) + first
        scale = amount / recipes[recipe]['output']
        net_sum = Fraction(0)
        count = 0
        first = None
        for index, (kind, ref, net) in enumerate(recipes[recipe]['components']):
            required = net * scale
            element = f'{recipe}:{index}'
            here = prefix + (element,)
            action = scopes.get(here)
            if action is None:
                next_suppressed = suppressed
            else:
                next_suppressed = action == SUPPRESS
            if kind == 'ingredient':
                if not next_suppressed and tag in ingredients[ref]['tags']:
                    net_sum += required
                    count += 1
                    if first is None:
                        first = list(here)
            else:
                child_net, child_count, child_first = walk(ref, required, next_suppressed, here)
                net_sum += child_net
                count += child_count
                if child_first is not None and first is None:
                    first = child_first
        return net_sum, count, first

    return walk(root, grams, False, ())


def _order_report(ingredients, recipes, order_id, ref, grams, thresholds, declarations):
    denominator = _total_net(recipes, ref, grams)
    evidence = []
    required_tags = []
    for tag in sorted(thresholds):
        scopes = declarations.get((order_id, tag), {})
        net, occurrences, first_path = _tag_stats(
            ingredients, recipes, ref, grams, tag, scopes)
        evidence.append({
            'tag': tag,
            'net': str(net),
            'occurrences': str(occurrences),
            'first_path': None if first_path is None else list(first_path),
        })
        if net >= thresholds[tag] * denominator:
            required_tags.append(tag)
    return {
        'id': order_id,
        'total_net': str(denominator),
        'required_tags': required_tags,
        'evidence': evidence,
    }


def run(data):
    ingredients, recipes, orders, thresholds, declarations, jobs = parse_preflight(data)
    order_reports = {}
    for order_id, ref, grams in orders:
        order_reports[order_id] = _order_report(
            ingredients, recipes, order_id, ref, grams, thresholds, declarations)

    print_jobs = []
    approved_count = 0
    for job in jobs:
        plate = set(job['tags'])
        rows = []
        approved = True
        for order_id in sorted(job['order_ids']):
            required = set(order_reports[order_id]['required_tags'])
            missing = sorted(required - plate)
            extra = sorted(plate - required)
            if missing or extra:
                approved = False
            rows.append({'order_id': order_id, 'missing': missing, 'extra': extra})
        if approved:
            approved_count += 1
        print_jobs.append({'id': job['id'], 'approved': approved, 'orders': rows})

    report = {
        'orders': [order_reports[order_id] for order_id in sorted(order_reports)],
        'print_jobs': sorted(print_jobs, key=lambda job: job['id']),
    }
    summary = {
        'orders': len(orders),
        'print_jobs': len(jobs),
        'approved': approved_count,
    }
    return {'report': report, 'summary': summary}
