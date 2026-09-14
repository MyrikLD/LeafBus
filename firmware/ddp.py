"""DDP receiver: the assembly as a network pixel sink (ESP32 / MicroPython).

One pixel = one panel, in chunk order. Senders: WLED sync, Hyperion,
xLights, LedFx, or a few lines of Python (tools/leafbus.py send).

DDP header (UDP port 4048), 10 bytes:
    0      flags: top two bits = version (01), bit 0 = PUSH
    1      sequence
    2      data type
    3      destination id
    4..7   data offset, big-endian
    8..9   data length, big-endian
    10..   pixels: 3 bytes (RGB) or 4 bytes (RGBW) each, chosen by length
"""
import socket
import panelbus

DDP_PORT = 4048
HDR = 10


class DDPReceiver:
    def __init__(self, bus, port=DDP_PORT):
        self.bus = bus
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', port))
        self.sock.settimeout(0)
        self.frames = 0

    def tick(self):
        """Drain the socket and apply the newest frame. Older queued frames
        are dropped rather than played late."""
        latest = None
        while True:
            try:
                pkt = self.sock.recv(1600)
            except OSError:
                break
            if not pkt or len(pkt) < HDR or (pkt[0] >> 6) != 1:
                continue
            length = (pkt[8] << 8) | pkt[9]
            latest = pkt[HDR:HDR + length]
        if latest:
            panelbus.apply_pixels(self.bus, latest)
            self.frames += 1
