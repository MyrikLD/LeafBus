"""Layout string grammar (PROTOCOL.md §7.1-7.5) against strings read from
real assemblies."""
import math

import pytest
import geometry as g

ONE = 'b1 04 95 40'
TWO = 'b1 b0 04 04 95 40'
CHAIN4 = 'b1 b0 04 b2 04 04 b2 04 95 40'
STAR4 = 'b1 b0 b1 04 04 b2 04 04 95 40'
FIVE_WITH_TRIANGLE = 'b1 b0 04 b0 e4 00 02 00 00 04 04 b2 95 04 40'
FOURTEEN = ('b1 b0 04 b2 b2 b0 b1 b1 04 e3 00 02 00 00 04 04 04 b2 04 04 04 '
            'b1 b1 b2 04 b1 b1 04 04 04 95 04 40')


def hx(s):
    return bytes.fromhex(s)


@pytest.mark.parametrize('raw, npanels', [
    (ONE, 1), (TWO, 2), (CHAIN4, 4), (STAR4, 4),
    (FIVE_WITH_TRIANGLE, 5), (FOURTEEN, 14),
])
def test_panel_count_and_full_consumption(raw, npanels):
    tree = g.parse(hx(raw))
    assert tree['npanels'] == npanels
    assert 'unparsed' not in tree


def test_node_byte_fields():
    tree = g.parse(hx(ONE))
    assert tree['hdr'] == 0xB1
    assert tree['edges'] == 3
    assert tree['root_edge'] == 1
    assert tree['shape'] == g.MINI_TRIANGLE
    assert tree['parent_edge'] is None


@pytest.mark.parametrize('code, n', [(0, 8), (1, 0), (2, 2), (3, 3), (6, 6), (7, 9)])
def test_connector_code_table(code, n):
    assert g.connectors(code) == n


def test_children_walk_connectors_clockwise_from_root_plus_one():
    tree = g.parse(hx(STAR4))
    # B1 root=1: the walk starts at connector 2, then 0
    assert sorted(tree['children']) == [0, 2]
    centre = tree['children'][2]
    assert centre['hdr'] == 0xB0
    assert sorted(centre['children']) == [1, 2]
    assert tree['children'][0]['shape'] == g.PSU


def test_separator_not_consumed_belongs_to_parent():
    # B1: conn 2 -> B0 (conn 2 -> B2), conn 0 -> B2 (conn 1 -> PSU).
    # After B0's single separator the next 04 closes B0 without being eaten,
    # and the following 04 advances B1 to connector 0.
    tree = g.parse(hx(CHAIN4))
    assert sorted(tree['children']) == [0, 2]
    b0 = tree['children'][2]
    assert b0['hdr'] == 0xB0 and list(b0['children']) == [2]
    assert b0['children'][2]['hdr'] == 0xB2 and not b0['children'][2]['children']
    b2 = tree['children'][0]
    assert b2['hdr'] == 0xB2 and list(b2['children']) == [1]
    assert b2['children'][1]['shape'] == g.PSU


def test_six_connector_node_owns_four_separators_and_is_a_triangle():
    tree = g.parse(hx(FIVE_WITH_TRIANGLE))
    tri = [p for p in g.panels(tree) if p['edges'] == 6]
    assert len(tri) == 1
    assert tri[0]['sep'] == [0x00, 0x02, 0x00, 0x00]
    assert tri[0]['shape'] == g.TRIANGLE


def test_subtype_from_separators():
    assert g.subtype(3, [0x04]) == g.MINI_TRIANGLE
    assert g.subtype(3, [0x08]) is None
    assert g.subtype(6, [0x00, 0x01, 0, 0]) == g.HEXAGON
    assert g.subtype(6, [0x00, 0x02, 0, 0]) == g.TRIANGLE
    assert g.subtype(6, [0x00, 0x03, 0, 0]) is None


def psu_at(raw):
    psu = g.layout(hx(raw))['psu']
    return psu['chunk'], psu['edge']


def test_psu_position_is_a_connector_of_a_panel():
    assert psu_at(ONE) == (0, 0)
    assert psu_at(TWO) == (1, 0)
    assert psu_at(CHAIN4) == (0, 1)
    assert psu_at(STAR4) == (3, 0)


def test_psu_and_link_points_lie_on_the_panel_edge():
    lay = g.layout(hx(CHAIN4))
    for p in lay['positions']:
        d = math.hypot(p['link'][0] - p['x'], p['link'][1] - p['y'])
        assert d == pytest.approx(67 / (2 * math.sqrt(3)), abs=0.01)
    psu = lay['psu']
    host = next(p for p in lay['positions'] if p['chunk'] == psu['chunk'])
    assert math.hypot(psu['x'] - host['x'], psu['y'] - host['y']) == pytest.approx(
        67 / (2 * math.sqrt(3)), abs=0.01)


def test_chunk_order_is_reverse_of_string_order():
    lay = g.layout(hx(CHAIN4))
    root = lay['tree']
    assert root['chunk'] == 3                      # panel next to the master
    assert root['children'][2]['chunk'] == 2
    assert [p['chunk'] for p in lay['positions']] == [0, 1, 2, 3]


def test_corrupted_root_edge_is_deferred_and_flagged():
    # EF: six connectors, root 7 does not exist
    raw = bytes([0xB1, 0xEF, 0x00, 0x02, 0x00, 0x00, 0x04, 0x95, 0x40])
    lay = g.layout(raw)
    tri = next(p for p in lay['positions'] if p['shape'] == g.TRIANGLE)
    assert tri['guessed'] is True
    assert 'unparsed' not in lay['tree']


def test_rejects_string_without_node_byte():
    with pytest.raises(ValueError):
        g.parse(bytes([0x04, 0x40]))
    with pytest.raises(ValueError):
        g.parse(b'')
