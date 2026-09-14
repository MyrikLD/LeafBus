"""E1.31 / sACN receiver (ESP32 / MicroPython).

One DMX channel = one color component, three consecutive channels (RGB) =
one panel, in chunk order; set e131_channels_per_panel to 4 in config.json
for RGBW. The count is fixed rather than guessed from the packet because
senders normally pad the universe to 512 channels. Understood by xLights,
QLC+, Vixen, Falcon, Hyperion and most lighting software.

The receiver joins the universe's multicast group 239.255.0.N, so senders
may address either the group or the board's IP. A failed join is not fatal:
access points that drop multicast to their Wi-Fi clients leave unicast
working.

E1.31 packet (UDP port 5568), 126-byte header:
      0..1    preamble 0x0010
      4..15   ACN identifier "ASC-E1.17"
     18..21   root layer vector (0x00000004)
     40..43   framing layer vector (0x00000002)
     44..107  source name
    108       priority
    111       sequence number
    113..114  universe, big-endian
    123..124  property value count = channels + 1
    125       DMX start code (0x00)
    126..     channel data
"""
import socket
import panelbus

E131_PORT = 5568
HDR = 126
ACN_ID = b'ASC-E1.17\x00\x00\x00'


def universe_multicast(universe):
    return '239.255.%d.%d' % ((universe >> 8) & 0xFF, universe & 0xFF)


class E131Receiver:
    def __init__(self, bus, universe=1, port=E131_PORT, per=3, join=True):
        self.bus = bus
        self.universe = universe
        self.per = per
        self.frames = 0
        self.last_seq = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(0)
        self.group = None
        if join:
            self._join(universe)

    def _join(self, universe):
        grp = universe_multicast(universe)
        try:
            mreq = bytes(int(x) for x in grp.split('.')) + bytes(4)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.group = grp
        except Exception as e:
            print('E1.31: join of %s failed (%s), unicast only' % (grp, e))

    @staticmethod
    def parse(pkt):
        """(universe, sequence, channel data) or None."""
        if len(pkt) < HDR + 1 or pkt[4:16] != ACN_ID or pkt[125] != 0x00:
            return None
        universe = (pkt[113] << 8) | pkt[114]
        seq = pkt[111]
        count = (pkt[123] << 8) | pkt[124]
        return universe, seq, pkt[HDR:HDR + max(0, count - 1)]

    def tick(self):
        latest = None
        while True:
            try:
                pkt = self.sock.recv(700)
            except OSError:
                break
            r = self.parse(pkt)
            if not r:
                continue
            universe, seq, data = r
            if universe != self.universe:
                continue
            # out-of-order packets are dropped (E1.31 §6.7.2)
            if self.last_seq is not None:
                d = (seq - self.last_seq) & 0xFF
                if d == 0 or d > 127:
                    continue
            self.last_seq = seq
            latest = data
        if latest:
            panelbus.apply_pixels(self.bus, latest, self.per)
            self.frames += 1
