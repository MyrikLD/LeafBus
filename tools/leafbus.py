#!/usr/bin/env python3
"""leafbus host tool

    leafbus.py layout <ip> [--json layout.json] [--svg layout.svg]
    leafbus.py state <ip>
    leafbus.py identify <ip> <chunk>
    leafbus.py brightness <ip> <0..255>
    leafbus.py off <ip>
    leafbus.py send <ip> <color> [<color> ...] [--e131]
    leafbus.py rainbow <ip> [--fps 30] [--speed 6] [--layout layout.json] [--e131]
"""
import argparse
import colorsys
import json
import math
import socket
import struct
import sys
import time
from typing import NoReturn

import httpx

DDP_PORT = 4048

SHAPE_NAMES = {7: 'hexagon', 8: 'triangle', 9: 'mini-triangle'}


def die(msg) -> NoReturn:
    sys.exit('leafbus: ' + msg)


# --- board HTTP -------------------------------------------------------------

def http_get(ip, path):
    return httpx.get(f'http://{ip}{path}').json()


def board_layout(ip):
    info = http_get(ip, '/layout')
    if 'error' in info:
        die('board reports: %s' % info['error'])
    if not info.get('positions'):
        die('board got no layout string: bus silent (panel power? DATA/GND? pull-up?)')
    return info


# --- senders ----------------------------------------------------------------

class DDPSender:
    """DDP (http://www.3waylabs.com/ddp/): 10-byte header, then pixels."""

    def __init__(self, ip):
        self.addr = (ip, DDP_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0

    def send(self, pixels):
        data = bytes(c for px in pixels for c in px)
        hdr = bytes([0x41, self.seq & 0x0F, 0, 1]) + struct.pack('>IH', 0, len(data))
        self.sock.sendto(hdr + data, self.addr)
        self.seq += 1

    def close(self):
        self.sock.close()


class E131Sender:
    """E1.31 via the `sacn` library, unicast to the board."""

    def __init__(self, ip, universe=1, fps=30):
        try:
            import sacn
        except ImportError:
            die('--e131 needs the sacn package: pip install sacn')
        self.universe = universe
        self.s = sacn.sACNsender(source_name='leafbus', fps=fps, universeDiscovery=False)
        self.s.start()
        self.s.activate_output(universe)
        self.s[universe].destination = ip

    def send(self, pixels):
        self.s[self.universe].dmx_data = tuple(c for px in pixels for c in px)

    def close(self):
        time.sleep(0.2)  # let the sender thread push the last frame
        self.s.stop()


def make_sender(a, fps=30):
    return E131Sender(a.ip, a.universe, fps) if a.e131 else DDPSender(a.ip)


NAMED = {'red': (255, 0, 0), 'green': (0, 255, 0), 'blue': (0, 0, 255),
         'yellow': (255, 200, 0), 'cyan': (0, 255, 255), 'magenta': (255, 0, 220),
         'white': (0, 0, 0, 255), 'warm': (255, 120, 0, 255), 'black': (0, 0, 0),
         'off': (0, 0, 0)}


def parse_color(s):
    s = s.strip().lower()
    if s in NAMED:
        return NAMED[s]
    h = s.lstrip('#')
    if len(h) in (6, 8) and all(c in '0123456789abcdef' for c in h):
        return tuple(int(h[i:i + 2], 16) for i in range(0, len(h), 2))
    parts = s.replace(',', ' ').split()
    if len(parts) in (3, 4) and all(p.isdigit() for p in parts):
        return tuple(min(255, int(p)) for p in parts)
    die('color %r: use a name (%s), rrggbb, rrggbbww or "r g b [w]"'
        % (s, ', '.join(sorted(NAMED))))


# --- layout -----------------------------------------------------------------

def print_tree(node, indent=0):
    pad = '  ' * indent
    if node.get('psu'):
        print(pad + 'PSU')
        return
    print('%schunk %-2d %-13s root=%s pos=(%.1f, %.1f) o=%d'
          % (pad, node['chunk'], SHAPE_NAMES.get(node['shape'], '?'), node['root_edge'],
             node['x'], node['y'], node['o']))
    for e, c in sorted(node.get('children', {}).items(), key=lambda kv: int(kv[0])):
        print('%s  connector %s ->' % (pad, e))
        print_tree(c, indent + 2)


def polygon(p):
    """Corner points of a panel from its centre, side, shape and orientation."""
    if p['shape'] == 7:
        radius, angles = p['side'], range(0, 360, 60)
    else:
        radius, angles = p['side'] / math.sqrt(3), (-30, 90, 210)
    return [(p['x'] + radius * math.cos(math.radians(a + p['o'])),
             p['y'] + radius * math.sin(math.radians(a + p['o']))) for a in angles]


def svg(info, path):
    """Panels with chunk numbers; a dot on the connector each panel is reached
    through (green: the bus master), a blue ring on the power supply."""
    ps = info['positions']
    master = len(ps) - 1
    polys = [(p, polygon(p)) for p in ps]
    xs = [x for _, pts in polys for x, _ in pts]
    ys = [y for _, pts in polys for _, y in pts]
    m = 20
    x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.1f %.1f %.1f %.1f" '
           'width="%d" height="%d">' % (x0, -y1, x1 - x0, y1 - y0, (x1 - x0) * 3, (y1 - y0) * 3),
           '<g transform="scale(1,-1)" stroke="#333" stroke-width="1" fill="#f6c343">']
    for p, pts in polys:
        out.append('<polygon points="%s"/>' % ' '.join('%.1f,%.1f' % xy for xy in pts))
    for p, _ in polys:
        if 'link' not in p:  # firmware older than the link field: no dots
            continue
        is_master = p['chunk'] == master
        out.append('<circle cx="%.1f" cy="%.1f" r="%d" fill="%s" stroke="none"/>'
                   % (p['link'][0], p['link'][1], 4 if is_master else 2,
                      '#2a2' if is_master else '#f80' if p.get('guessed') else '#c00'))
    psu = info.get('psu')
    if psu and 'x' in psu:
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="none" stroke="#06c" '
                   'stroke-width="1.5"/>' % (psu['x'], psu['y']))
    out.append('</g>')
    for p, _ in polys:
        out.append('<text x="%.1f" y="%.1f" font-size="12" text-anchor="middle" '
                   'dominant-baseline="middle" font-family="sans-serif">%d</text>'
                   % (p['x'], -p['y'], p['chunk']))
    out.append('</svg>')
    with open(path, 'w') as f:
        f.write('\n'.join(out))


def cmd_layout(a):
    info = board_layout(a.ip)
    print('layout string:', info['raw'], '(unstable)' if info.get('unstable') else '')
    print_tree(info['tree'])
    print('power supply:', info['psu'])

    if a.json:
        with open(a.json, 'w') as f:
            json.dump({k: info[k] for k in ('panels', 'raw', 'psu', 'positions')}, f, indent=1)
        print('written', a.json)
    if a.svg:
        svg(info, a.svg)
        print('written', a.svg)


# --- simple board commands --------------------------------------------------

def cmd_state(a):
    print(json.dumps(http_get(a.ip, '/state'), indent=1))


def cmd_identify(a):
    print(json.dumps(http_get(a.ip, '/identify/%d' % a.chunk)))


def cmd_brightness(a):
    print(json.dumps(http_get(a.ip, '/brightness/%d' % a.value)))


def cmd_off(a):
    print(json.dumps(http_get(a.ip, '/off')))


def cmd_send(a):
    n = http_get(a.ip, '/layout')['panels']
    cols = [parse_color(c) for c in a.colors]
    if len(cols) == 1:
        cols = cols * n
    per = max(len(c) for c in cols)
    cols = [tuple(c) + (0,) * (per - len(c)) for c in cols]
    s = make_sender(a)
    for _ in range(a.repeat):
        s.send(cols)
        time.sleep(0.05)
    s.close()
    print('sent %d panels via %s' % (len(cols), 'E1.31' if a.e131 else 'DDP'))


def hue_by_angle(path):
    with open(path) as f:
        lay = json.load(f)
    pos = {p['chunk']: (p['x'], p['y']) for p in lay['positions']}
    cx = sum(x for x, _ in pos.values()) / len(pos)
    cy = sum(y for _, y in pos.values()) / len(pos)
    return {i: (math.atan2(y - cy, x - cx) / (2 * math.pi)) % 1.0
            for i, (x, y) in pos.items()}


def cmd_rainbow(a):
    n = a.panels or http_get(a.ip, '/layout')['panels']
    hues = hue_by_angle(a.layout) if a.layout else {}
    s = make_sender(a, fps=int(a.fps))
    print('rainbow: %d panels, %s, %.0f fps%s -- Ctrl-C to stop'
          % (n, 'E1.31' if a.e131 else 'DDP', a.fps,
             ', hue by angle around the assembly' if hues else ''))
    t0 = time.time()
    frames = 0
    try:
        while True:
            ph = (time.time() - t0) / a.speed
            frame = []
            for p in range(n):
                r, g, b = colorsys.hsv_to_rgb((ph + hues.get(p, p / n)) % 1.0, 1.0, a.dim)
                frame.append((int(r * 255), int(g * 255), int(b * 255)))
            s.send(frame)
            frames += 1
            time.sleep(1.0 / a.fps)
    except KeyboardInterrupt:
        print('\nstopped after %d frames' % frames)
    finally:
        s.close()


# --- argument parsing -------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog='leafbus.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('layout', help='tree, chunk order and coordinates from the board')
    p.add_argument('ip')
    p.add_argument('--json', metavar='FILE', help='write positions + psu as JSON')
    p.add_argument('--svg', metavar='FILE', help='draw the assembly')
    p.set_defaults(fn=cmd_layout)

    p = sub.add_parser('state', help='touch state and frame counters')
    p.add_argument('ip')
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser('identify', help='light one panel white')
    p.add_argument('ip')
    p.add_argument('chunk', type=int)
    p.set_defaults(fn=cmd_identify)

    p = sub.add_parser('brightness', help='hardware brightness 0..255')
    p.add_argument('ip')
    p.add_argument('value', type=int)
    p.set_defaults(fn=cmd_brightness)

    p = sub.add_parser('off', help='all panels black')
    p.add_argument('ip')
    p.set_defaults(fn=cmd_off)

    p = sub.add_parser('send', help='send one frame: one color for all, or one per panel')
    p.add_argument('ip')
    p.add_argument('colors', nargs='+', metavar='color')
    p.add_argument('--e131', action='store_true')
    p.add_argument('--universe', type=int, default=1)
    p.add_argument('--repeat', type=int, default=3, help='send the frame N times (UDP)')
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser('rainbow', help='stream a rainbow from the host')
    p.add_argument('ip')
    p.add_argument('--panels', type=int, default=0, help='0 = ask the board')
    p.add_argument('--fps', type=float, default=30)
    p.add_argument('--speed', type=float, default=6, help='seconds per revolution')
    p.add_argument('--dim', type=float, default=1.0, help='value multiplier 0..1')
    p.add_argument('--layout', metavar='FILE', help='layout JSON: hue follows the angle around the centre')
    p.add_argument('--e131', action='store_true')
    p.add_argument('--universe', type=int, default=1)
    p.set_defaults(fn=cmd_rainbow)

    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
