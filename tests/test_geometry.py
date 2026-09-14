"""Coordinates from the layout string (PROTOCOL.md §7.6): the tiling must
close (child connector on parent connector) and never overlap."""
import itertools
import math

import pytest
import geometry as g

from test_layout_parser import (CHAIN4, STAR4, FIVE_WITH_TRIANGLE, FOURTEEN,
                                TWO, hx)

INRADIUS_MINI = 67 / (2 * math.sqrt(3))
INRADIUS_TRI = 134 / (2 * math.sqrt(3))
APOTHEM_HEX = 67 * math.sqrt(3) / 2


def connector_world(p, e):
    x, y = g.rot(*g.edge_vec(p['shape'], e), p['o'])
    return p['x'] + x, p['y'] + y


def walk(node, out=None):
    out = [] if out is None else out
    for e, c in node['children'].items():
        if c['shape'] != g.PSU:
            out.append((node, e, c))
            walk(c, out)
    return out


def test_edge_vectors_have_the_documented_lengths():
    for e in range(3):
        assert math.hypot(*g.edge_vec(g.MINI_TRIANGLE, e)) == pytest.approx(INRADIUS_MINI)
    for e in range(6):
        assert math.hypot(*g.edge_vec(g.HEXAGON, e)) == pytest.approx(APOTHEM_HEX)
        tx, ty = g.edge_vec(g.TRIANGLE, e)
        # two connectors per side, a quarter side either way from the midpoint
        assert math.hypot(tx, ty) == pytest.approx(math.hypot(33.5, INRADIUS_TRI))


def test_root_panel_at_origin():
    lay = g.layout(hx(TWO))
    root = lay['tree']
    assert (root['x'], root['y'], root['o']) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize('raw', [TWO, CHAIN4, STAR4, FIVE_WITH_TRIANGLE, FOURTEEN])
def test_child_connector_lands_on_parent_connector(raw):
    lay = g.layout(hx(raw))
    for parent, pe, child in walk(lay['tree']):
        px, py = connector_world(parent, pe)
        cx, cy = connector_world(child, child['root_edge'])
        assert (cx, cy) == pytest.approx((px, py), abs=1e-6)


@pytest.mark.parametrize('raw', [CHAIN4, STAR4, FIVE_WITH_TRIANGLE, FOURTEEN])
def test_panels_do_not_overlap(raw):
    ps = g.panels(g.layout(hx(raw))['tree'])
    for a, b in itertools.combinations(ps, 2):
        d = math.hypot(a['x'] - b['x'], a['y'] - b['y'])
        # two mini triangles sharing a side are exactly 2 inradii apart
        assert d > 2 * INRADIUS_MINI - 1e-6


def test_orientations_are_multiples_of_sixty():
    for p in g.layout(hx(FOURTEEN))['positions']:
        assert p['o'] % 60 == 0


def test_known_positions_of_the_two_panel_assembly():
    # B0 on connector 2 of B1: the second panel is flipped and sits up-right
    lay = g.layout(hx(TWO))
    pos = {p['chunk']: p for p in lay['positions']}
    assert (pos[0]['x'], pos[0]['y'], pos[0]['o']) == (33.5, 19.34, 300)


def test_triangle_occupies_the_footprint_of_four_minis():
    lay = g.layout(hx(FIVE_WITH_TRIANGLE))
    tri = next(p for p in g.panels(lay['tree']) if p['shape'] == g.TRIANGLE)
    minis = [p for p in g.panels(lay['tree']) if p['shape'] == g.MINI_TRIANGLE]
    for m in minis:
        d = math.hypot(m['x'] - tri['x'], m['y'] - tri['y'])
        assert d > INRADIUS_MINI + INRADIUS_TRI - 1e-6


def test_positions_are_sorted_by_chunk_and_rounded():
    lay = g.layout(hx(FOURTEEN))
    chunks = [p['chunk'] for p in lay['positions']]
    assert chunks == list(range(14))
    for p in lay['positions']:
        assert p['x'] == round(p['x'], 2) and p['y'] == round(p['y'], 2)
        assert p['side'] == g.SIDE[p['shape']]
