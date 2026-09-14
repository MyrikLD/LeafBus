"""Bus master for Nanoleaf Shapes panels (ESP32 / MicroPython).

Layer 1 of the firmware: frames in, bytes out. Frame formats and timing are
specified in PROTOCOL.md; section numbers below refer to it. Tree parsing
and panel coordinates live in geometry.py.

Wiring (PROTOCOL.md §3):

    ESP32 TX (open drain) --+---- DATA pad of a panel
    ESP32 RX ---------------+
    1-2 kOhm pull-up from DATA to +3.3 V
    ESP32 GND --------------- panel GND
    the 42 V supply pad is left alone

TX must be open-drain: a push-pull output holds the line high when idle and
the panel cannot answer (PROTOCOL.md §3.1).
"""
from machine import UART, mem32
import time

BAUD = 1_000_000
TX_PIN, RX_PIN = 17, 16

# Original ESP32. The S2/S3/C3 families have a different GPIO base address.
GPIO_PIN0_REG = 0x3FF44088
PAD_DRIVER = 1 << 2          # 1 = open drain

# frame types (PROTOCOL.md §4.1)
ROOT_DETECT = 0x00
LAYOUT_DETECT = 0x80
BULK_PULL = 0xC0
BULK_PUSH = 0xE0
NODE_CMD = 0xF8
GLOBAL_CMD = 0xFC
FLOOD = 0xFE

TERMINATOR = 0x40
PSU_CODE = 1                 # connector-count code of the power supply node
HOTSWAP = 0xCC               # trailing byte of a bulk pull reply (§8)

# global / node commands (§6.1): cmd < 0x80 is SET without reply
CMD_BRIGHTNESS = 0x04
CMD_COLOR_LEGACY = 0x06      # no effect on Shapes
CMD_TOUCH_ENABLE = 0x07
CMD_KEEPALIVE = 0x08
CMD_LOW_POWER = 0x09
GET_VERSION = 0x81
GET_UID = 0x82

SKIP = bytes([0x01, 0xFF])   # bulk push chunk that leaves a panel unchanged


class BusError(Exception):
    pass


def set_open_drain(pin):
    """Switch the pad to open drain while keeping the UART routing.
    Must run after UART(...): UART initialisation clears the bit."""
    mem32[GPIO_PIN0_REG + 4 * pin] |= PAD_DRIVER


def is_open_drain(pin):
    return bool(mem32[GPIO_PIN0_REG + 4 * pin] & PAD_DRIVER)


def _tx_done(uart, nbytes, baud=BAUD):
    """Wait until the transmitter is physically empty."""
    try:
        uart.flush()
    except AttributeError:
        time.sleep_us(int(nbytes * 10 * 1_000_000 / baud) + 50)


def chunk(r, g, b, w=0, transition=1):
    """Single-zone panel chunk: 05 T R G B W (§6.2)."""
    return bytes([0x05, transition & 0xFF, r & 0xFF, g & 0xFF,
                  b & 0xFF, w & 0xFF])


class PanelBus:
    def __init__(self, tx=TX_PIN, rx=RX_PIN, baud=BAUD, open_drain=True):
        self.uart = UART(1, baudrate=baud, bits=8, parity=None, stop=1,
                         tx=tx, rx=rx, rxbuf=1024, timeout=50)
        if open_drain:
            set_open_drain(tx)
            if not is_open_drain(tx):
                raise BusError('open-drain bit did not stick: check '
                               'GPIO_PIN0_REG for your ESP32 variant')
        self.npanels = 1
        self.nodes = []
        self.layout_unstable = False

    # --- transport ------------------------------------------------------
    def _xact(self, frame, expect=0, wait_ms=40):
        """Send one frame and read the reply if one is expected.

        expect: number of reply bytes, 0 for none, -1 for "up to TERMINATOR".
        The master hears its own echo on the single wire; it is stripped.
        """
        while self.uart.any():
            self.uart.read()
        self.uart.write(frame)
        _tx_done(self.uart, len(frame))
        if expect == 0:
            return b''
        deadline = time.ticks_add(time.ticks_ms(), wait_ms)
        buf = b''
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            got = self.uart.read()
            if got:
                buf += got
                body = buf[len(frame):] if buf[:len(frame)] == frame else buf
                if expect == -1:
                    if TERMINATOR in body:
                        return body
                elif len(body) >= expect:
                    return body
            time.sleep_us(200)
        return buf[len(frame):] if buf[:len(frame)] == frame else buf

    # --- enumeration (§5) ----------------------------------------------
    def root_detect(self):
        self._xact(bytes([ROOT_DETECT]), expect=0)

    def layout_detect(self, wait_ms=None):
        """Return the raw layout string. The reply is relayed hop by hop, so
        the window grows with the panel count; one retry with a long window."""
        if wait_ms is None:
            wait_ms = 200 + 40 * self.npanels
        raw = self._xact(bytes([LAYOUT_DETECT]), expect=-1, wait_ms=wait_ms)
        if not raw or TERMINATOR not in raw:
            raw = self._xact(bytes([LAYOUT_DETECT]), expect=-1, wait_ms=1500)
        return raw

    @staticmethod
    def parse_node(b):
        if not (b & 0x80):
            raise ValueError('invalid first byte in layout string')
        return {'code': (b >> 4) & 7, 'root_edge': b & 7}

    def enumerate(self, tries=6):
        """Root side detect + layout detect. Returns the layout string
        (including the terminator) and sets self.npanels / self.nodes.

        The head of the string arrives corrupted on large assemblies
        (§9.2), so the string is read until one value repeats.
        """
        seen = {}
        raw = b''
        self.layout_unstable = False
        for _ in range(tries):
            self.root_detect()
            time.sleep_ms(20)
            raw = self.layout_detect()
            if not raw or TERMINATOR not in raw:
                continue
            raw = raw[:raw.index(TERMINATOR) + 1]
            seen[raw] = seen.get(raw, 0) + 1
            if seen[raw] >= 2:
                break
        else:
            self.layout_unstable = True
            if seen:
                raw = max(seen, key=seen.get)
        if not raw or TERMINATOR not in raw:
            raise BusError('no layout string: bus silent (panel power? '
                           'DATA/GND wiring? pull-up?)')
        body = raw[:raw.index(TERMINATOR)]
        nodes = [self.parse_node(b) for b in body
                 if b & 0x80 and (b >> 4) & 7 != PSU_CODE]
        if not nodes:
            raise BusError('layout string has no panel nodes: %s' % raw.hex())
        self.nodes = nodes
        self.npanels = len(nodes)
        return raw

    # --- polling (§6.3) -------------------------------------------------
    def bulk_pull(self, wait_ms=None):
        """2 bytes per panel: status and touch."""
        if wait_ms is None:
            wait_ms = 40 + 20 * self.npanels
        return self._xact(bytes([BULK_PULL]), expect=2 * self.npanels,
                          wait_ms=wait_ms)

    def hold(self, ms, period_ms=30):
        """Keep the bus polled for a while, as the stock controller does.
        Without polling only the first color frame is applied (§4.2)."""
        t0 = time.ticks_ms()
        answered = 0
        while time.ticks_diff(time.ticks_ms(), t0) < ms:
            if self.bulk_pull():
                answered += 1
            time.sleep_ms(period_ms)
        return answered

    # --- color (§6.2) --------------------------------------------------
    def push(self, chunks):
        self._xact(bytes([BULK_PUSH, 0x03]) + b''.join(chunks))

    def push_colors(self, colors):
        """One bulk push frame for the whole assembly.

        colors: one entry per panel in chunk order:
            (r, g, b)              W = 0, transition 1
            (r, g, b, w)
            (r, g, b, w, t)
            None                   leave this panel unchanged
        """
        out = bytearray([BULK_PUSH, 0x03])
        for c in colors:
            if c is None:
                out += SKIP
            else:
                w = c[3] if len(c) > 3 else 0
                t = c[4] if len(c) > 4 else 1
                out += chunk(c[0], c[1], c[2], w, t)
        self._xact(bytes(out), expect=0)
        return bytes(out)

    def fill(self, r, g, b, w=0, transition=1):
        self.push_colors([(r, g, b, w, transition)] * self.npanels)

    def set_color(self, r, g, b, w=0, transition=1, hold_ms=200):
        """Set one color on every panel and keep the bus polled so that the
        frame is applied."""
        self.fill(r, g, b, w, transition)
        return self.hold(hold_ms)

    # --- global commands (§6.1) ----------------------------------------
    def brightness(self, value):
        self._xact(bytes([GLOBAL_CMD, CMD_BRIGHTNESS, value & 0xFF]))

    def keepalive(self):
        self._xact(bytes([GLOBAL_CMD, CMD_KEEPALIVE, 0x00]))

    def touch_enable(self, on=True):
        self._xact(bytes([GLOBAL_CMD, CMD_TOUCH_ENABLE, 1 if on else 0]))

    def node_get(self, node_id, cmd, expect, wait_ms=400):
        """Addressed GET (§5.4). Returns the raw reply including the
        per-hop acknowledgement bytes."""
        frame = bytes([NODE_CMD, node_id & 0xFF, (node_id >> 8) & 0xFF, cmd])
        return self._xact(frame, expect=expect, wait_ms=wait_ms)

    def uid(self, node_id):
        raw = self.node_get(node_id, GET_UID, expect=18 + node_id + 1)
        if 0x01 not in raw:
            return None
        return raw[raw.index(0x01) + 1:]


def apply_pixels(bus, data, per=None):
    """Map a pixel stream onto the panels: one pixel per panel in chunk
    order, 3 bytes (RGB) or 4 bytes (RGBW) per pixel. With per=None the
    format is chosen by length. Panels beyond the end of the stream are
    left unchanged."""
    n = bus.npanels
    if per is None:
        per = 4 if len(data) >= 4 * n else 3
    cols = []
    for i in range(n):
        o = i * per
        if o + per > len(data):
            cols.append(None)
            continue
        w = data[o + 3] if per == 4 else 0
        cols.append((data[o], data[o + 1], data[o + 2], w, 1))
    bus.push_colors(cols)
    return cols


def line_check(bus, tries=20):
    """Edge-quality check (§3.1): 0x55/0xAA alternate every bit, the worst
    case for a pull-up driven line. A distorted echo means the pull-up is
    too weak or the wires too long. Panels ignore these bytes."""
    pattern = bytes([0x55, 0xAA, 0x55, 0xAA, 0x0F, 0xF0])
    ok = 0
    for _ in range(tries):
        while bus.uart.any():
            bus.uart.read()
        bus.uart.write(pattern)
        _tx_done(bus.uart, len(pattern))
        time.sleep_ms(5)
        got = bus.uart.read() or b''
        if got[:len(pattern)] == pattern:
            ok += 1
        else:
            print('distorted echo:', got)
    print('clean echo: %d/%d' % (ok, tries))
    if ok < tries:
        print('-> lower the pull-up (1 kOhm) or shorten the wires')
    return ok == tries
