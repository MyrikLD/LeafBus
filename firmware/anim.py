"""Stand-alone animations on the board, no network needed.

Frames are pushed continuously, which keeps the bus busy, so no separate
polling is required while an animation runs.

    import anim; anim.rainbow()
"""
import time
import config
import panelbus


def hsv(h, s=255, v=255):
    """Integer HSV -> RGB. h in 0..1535 (six sectors of 256)."""
    h %= 1536
    sec, f = h >> 8, h & 0xFF
    p = (v * (255 - s)) // 255
    q = (v * (255 - (s * f) // 255)) // 255
    t = (v * (255 - (s * (255 - f)) // 255)) // 255
    return ((v, t, p), (q, v, p), (p, v, t),
            (p, q, v), (t, p, v), (v, p, q))[sec]


def open_bus():
    cfg = config.load()
    bus = panelbus.PanelBus(tx=cfg['uart']['tx'], rx=cfg['uart']['rx'])
    bus.enumerate()
    bus.brightness(cfg['brightness'])
    return bus


def rainbow(bus=None, speed=6, spread=1536, fps=25):
    """Hue runs along the chunk order.

    speed   hue step per frame
    spread  hue span across the whole assembly (1536 = full circle)
    """
    bus = bus or open_bus()
    n = bus.npanels
    step = spread // max(1, n)
    delay = max(0, 1000 // fps)
    phase = 0
    while True:
        bus.push_colors([hsv(phase + i * step) for i in range(n)])
        phase = (phase + speed) % 1536
        if delay:
            time.sleep_ms(delay)


def solid(bus, r, g, b, w=0):
    bus.push_colors([(r, g, b, w)] * bus.npanels)
