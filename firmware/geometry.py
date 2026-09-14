"""Layout string -> assembly tree, chunk order and panel coordinates.

Layer 2 of the firmware: pure functions over bytes, no hardware access. Runs
unchanged on the host (tests, tools) and in MicroPython. Grammar, subtype
encoding and the geometry formulas are specified in PROTOCOL.md §7.

Coordinates are millimetres as in the Nanoleaf API: x right, y up,
orientation in degrees counter-clockwise. The root panel is at (0, 0)
with orientation 0.
"""
import math

TERMINATOR = 0x40
PSU_CODE = 1

# connector-count code (bits 6..4 of the node byte) -> connectors (§7.1)
_CONNECTORS = {0: 8, 1: 0, 7: 9}

# shape ids as in the Nanoleaf API
HEXAGON = 7
TRIANGLE = 8
MINI_TRIANGLE = 9
PSU = 'psu'

SIDE = {HEXAGON: 67.0, TRIANGLE: 134.0, MINI_TRIANGLE: 67.0}
SHAPE_NAMES = {HEXAGON: 'hexagon', TRIANGLE: 'triangle',
               MINI_TRIANGLE: 'mini-triangle', PSU: 'psu'}


def connectors(code):
    return _CONNECTORS.get(code, code)


def subtype(edges, sep):
    """Panel subtype from the separator bytes (§7.3)."""
    if edges == 3:
        sub = (sep[0] >> 2) & 7 if sep else 0
        return MINI_TRIANGLE if sub < 2 else None
    if edges == 6:
        sub = sep[1] & 7 if len(sep) > 1 else 0
        return {1: HEXAGON, 2: TRIANGLE}.get(sub)
    return None


def parse(raw):
    """Parse a layout string into a tree (§7.2).

    Node: {'hdr', 'edges', 'root_edge', 'shape', 'sep', 'parent_edge',
           'children': {connector: node}, 'index'}
    'index' is the panel's position in the string; the power supply has none.
    A root connector outside 0..edges-1 (corrupted byte, §9.2) is stored as
    None and resolved later by place().
    """
    body = bytes(raw)
    if TERMINATOR in body:
        body = body[:body.index(TERMINATOR)]
    if not body or not (body[0] & 0x80):
        raise ValueError('invalid first byte in layout string')
    counter = [0]

    def node(i, parent_edge):
        hdr = body[i]
        i += 1
        code = (hdr >> 4) & 7
        n = {'hdr': hdr, 'root_edge': hdr & 7, 'parent_edge': parent_edge,
             'sep': [], 'children': {}}
        if code == PSU_CODE:
            n['edges'] = 1
            n['shape'] = PSU
            return n, i
        edges = connectors(code)
        n['edges'] = edges
        n['index'] = counter[0]
        counter[0] += 1
        root = n['root_edge']
        if root >= edges:
            n['root_edge'] = None
            e = 0
        else:
            e = (root + 1) % edges
        seps = 0
        while i < len(body):
            b = body[i]
            if b & 0x80:
                child, i = node(i, e)
                n['children'][e] = child
            else:
                seps += 1
                if seps == edges - 1:
                    break
                n['sep'].append(b)
                e = (e + 1) % edges
                i += 1
        n['shape'] = subtype(edges, n['sep'])
        return n, i

    root, end = node(0, None)
    root['npanels'] = counter[0]
    if end != len(body):
        root['unparsed'] = body[end:]
    return root


# --- geometry (§7.6) --------------------------------------------------------

def rot(x, y, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def side_of(shape, e):
    """Connector -> geometric side. A Triangle has two connectors per side."""
    return ((e + 1) >> 1) % 3 if shape == TRIANGLE else e


def edge_angle(shape, e):
    """Angle of the side carrying connector e. Connectors are numbered
    clockwise, so the angle grows with e while rotations stay CCW."""
    step = 60 if shape == HEXAGON else 120
    return step * side_of(shape, e)


def edge_vec(shape, e):
    """Vector from the panel centre to connector e, panel frame."""
    s = SIDE[shape]
    if shape == HEXAGON:
        return rot(0, -s * math.sqrt(3) / 2, -edge_angle(shape, e))
    r = s / (2 * math.sqrt(3))
    x = 0.0
    if shape == TRIANGLE:
        x = -s / 4 if e in (0, 2, 4) else s / 4
    return rot(x, -r, -edge_angle(shape, e))


def _attach(p, pe, c, ce):
    """Place child c with its connector ce on connector pe of parent p."""
    delta = (180 + edge_angle(c['shape'], ce) - edge_angle(p['shape'], pe)) % 360
    ax, ay = edge_vec(p['shape'], pe)
    bx, by = rot(*edge_vec(c['shape'], ce), delta)
    ox, oy = rot(ax - bx, ay - by, p['o'])
    c['x'], c['y'] = p['x'] + ox, p['y'] + oy
    c['o'] = (p['o'] + delta) % 360


def place(root):
    """Assign x, y (mm) and o (degrees) to every node.

    Nodes whose root connector is unknown are placed in a second pass: every
    connector is tried and the one that puts the panel farthest from the
    panels already placed wins (the others overlap the assembly).
    """
    root['x'] = root['y'] = root['o'] = 0.0
    placed = []
    deferred = []

    def walk(p):
        placed.append(p)
        for pe, c in p['children'].items():
            if c['shape'] == PSU or p['shape'] is None or c['shape'] is None:
                c['x'], c['y'], c['o'] = p['x'], p['y'], p['o']
            elif c['root_edge'] is None:
                deferred.append((p, pe, c))
                continue
            else:
                _attach(p, pe, c, c['root_edge'])
            walk(c)
    walk(root)

    for p, pe, c in deferred:
        best = None
        for ce in range(c['edges']):
            _attach(p, pe, c, ce)
            others = [q for q in placed if q is not p and q['shape'] != PSU]
            d = min(math.sqrt((c['x'] - q['x']) ** 2 + (c['y'] - q['y']) ** 2)
                    for q in others) if others else 0
            if best is None or d > best[0] + 1e-6:
                best = (d, ce)
        c['guessed_edge'] = best[1]
        _attach(p, pe, c, best[1])
        walk(c)
    return root


def panels(root):
    """Flat list of panels (no PSU) in string order."""
    out = []

    def walk(n):
        if n['shape'] != PSU:
            out.append(n)
        for e in sorted(n['children']):
            walk(n['children'][e])
    walk(root)
    return out


def _connector_xy(p, e):
    """World position of connector e of a placed panel, rounded."""
    x, y = rot(*edge_vec(p['shape'], e), p['o'])
    return [round(p['x'] + x, 2), round(p['y'] + y, 2)]


def layout(raw):
    """Everything derived from one layout string:

    {'psu': {'chunk', 'edge', 'x', 'y'} or None,
     'positions': [{'chunk', 'x', 'y', 'o', 'shape', 'side', 'link'[, 'guessed']}, ...]
                  sorted by chunk, like positionData in the Nanoleaf API;
                  'link' is the connector through which the panel is reached
                  (on the master panel: where the bus master is attached),
     'tree': the parsed tree with coordinates}

    chunk = npanels - 1 - index: the panel next to the master is the first
    node of the string and gets the last chunk (§5.3).
    """
    root = place(parse(raw))
    n = root['npanels']
    ps = panels(root)
    for p in ps:
        p['chunk'] = n - 1 - p['index']
    psu = None

    def find_psu(node):
        nonlocal psu
        for e, c in node['children'].items():
            if c['shape'] == PSU:
                psu = {'chunk': node.get('chunk'), 'edge': e}
            else:
                find_psu(c)
    find_psu(root)
    if psu:
        host = next(p for p in ps if p['chunk'] == psu['chunk'])
        psu['x'], psu['y'] = _connector_xy(host, psu['edge'])
    positions = []
    for p in sorted(ps, key=lambda p: p['chunk']):
        edge = p.get('guessed_edge', 0) if p['root_edge'] is None else p['root_edge']
        d = {'chunk': p['chunk'], 'x': round(p['x'], 2), 'y': round(p['y'], 2),
             'o': int(p['o']), 'shape': p['shape'], 'side': SIDE.get(p['shape']),
             'link': _connector_xy(p, edge)}
        if p['root_edge'] is None:
            d['guessed'] = True
        positions.append(d)
    return {'psu': psu, 'positions': positions, 'tree': root}


def tree_text(node, indent=0):
    """Human-readable tree, one node per line."""
    pad = '  ' * indent
    if node['shape'] == PSU:
        return pad + 'PSU\n'
    root = node['root_edge']
    if root is None:
        root = '?->%s' % node.get('guessed_edge', '?')
    s = '%s0x%02x %-13s connectors=%d root=%s chunk=%s pos=(%.1f, %.1f) o=%d\n' % (
        pad, node['hdr'], SHAPE_NAMES.get(node['shape'], '?'), node['edges'], root,
        node.get('chunk'), node.get('x', 0), node.get('y', 0), node.get('o', 0))
    for e in sorted(node['children']):
        c = node['children'][e]
        s += pad + '  connector %d -> ' % e + tree_text(c, indent + 1).lstrip()
    return s


def polygon(p):
    """Corner points of a placed panel, for drawing."""
    s = SIDE[p['shape']]
    if p['shape'] == HEXAGON:
        radius, angles = s, range(0, 360, 60)
    else:
        radius, angles = s / math.sqrt(3), (-30, 90, 210)
    return [(p['x'] + x, p['y'] + y) for x, y in
            (rot(radius * math.cos(math.radians(a)),
                 radius * math.sin(math.radians(a)), p['o']) for a in angles)]
