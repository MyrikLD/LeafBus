"""HTTP server: topology, state and a few controls as JSON.

Non-blocking; tick() runs in the same loop as the network receivers and
never stalls the frame stream.

    GET /              endpoint list
    GET /layout        tree with coordinates, chunk order, power supply, raw string
    GET /state         touch per panel, frame counters
    GET /identify/N    light one panel white (match chunk index to a physical panel)
    GET /brightness/N  hardware brightness 0..255 (FC 04)
    GET /off           all panels black
"""
import socket

import ujson

import geometry


class WebServer:
    def __init__(self, bus, port=80, sources=None):
        self.bus = bus
        self.sources = sources or {}  # {'ddp': receiver, 'e131': receiver}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', port))
        self.sock.listen(2)
        self.sock.settimeout(0)
        self.raw_layout = b''

    @staticmethod
    def _json_tree(node):
        if node['shape'] == geometry.PSU:
            return {'psu': True}
        out = {'chunk': node['chunk'], 'connectors': node['edges'],
               'root_edge': node['root_edge'], 'shape': node['shape'],
               'x': round(node['x'], 1), 'y': round(node['y'], 1), 'o': int(node['o'])}
        kids = {str(e): WebServer._json_tree(c) for e, c in node['children'].items()}
        if kids:
            out['children'] = kids
        return out

    def layout(self):
        self.raw_layout = self.bus.enumerate()
        data = {'panels': self.bus.npanels, 'raw': self.raw_layout.hex(),
                'unstable': self.bus.layout_unstable}
        try:
            lay = geometry.layout(self.raw_layout)
        except ValueError as e:
            data['error'] = str(e)
            return data
        tree = lay.pop('tree')
        data.update(lay)
        data['tree'] = self._json_tree(tree)
        return data

    def state(self):
        raw = self.bus.bulk_pull() or b''
        n = self.bus.npanels
        status = [raw[i] for i in range(0, 2 * n, 2)] if len(raw) >= 2 * n else []
        d = {'panels': n,
             'touch': status[::-1],
             'hotplug': len(raw) > 2 * n,
             'raw': raw.hex()}
        for name, src in self.sources.items():
            d[name + '_frames'] = src.frames
        return d

    def _handle(self, path):
        path = path.split('?', 1)[0]
        if path.startswith('/layout'):
            return 200, self.layout()
        if path.startswith('/state'):
            return 200, self.state()
        if path.startswith('/identify'):
            seg = path[len('/identify'):].strip('/')
            n = self.bus.npanels
            if not seg.isdigit() or not (0 <= int(seg) < n):
                return 400, {'error': '/identify/N with N in 0..%d' % (n - 1)}
            cols = [(0, 0, 0)] * n
            cols[int(seg)] = (255, 255, 255, 255)
            self.bus.push_colors(cols)
            return 200, {'identify': int(seg)}
        if path.startswith('/brightness'):
            seg = path[len('/brightness'):].strip('/')
            if not seg.isdigit():
                return 400, {'error': '/brightness/N with N in 0..255'}
            v = min(255, int(seg))
            self.bus.brightness(v)
            return 200, {'brightness': v}
        if path.startswith('/off'):
            self.bus.push_colors([(0, 0, 0)] * self.bus.npanels)
            return 200, {'off': True}
        return 200, {'endpoints': ['/layout', '/state', '/identify/N',
                                   '/brightness/N', '/off'],
                     'panels': self.bus.npanels}

    def tick(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(1)
            req = conn.recv(512)
            parts = req.split(b' ') if req else []
            path = parts[1].decode() if len(parts) > 1 else '/'
            try:
                code, body = self._handle(path)
            except Exception as e:
                code, body = 500, {'error': str(e)}
            reason = {200: 'OK', 400: 'Bad Request', 500: 'Internal Server Error'}[code]
            conn.send('HTTP/1.0 %d %s\r\nContent-Type: application/json\r\n'
                      'Access-Control-Allow-Origin: *\r\n\r\n' % (code, reason))
            conn.send(ujson.dumps(body))
        except Exception as e:
            print('web:', e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
