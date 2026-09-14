"""Application: Wi-Fi + bus + receivers in one loop.

Two details that matter for throughput on MicroPython:

* Sockets are waited on with uselect.poll() rather than drained until
  OSError. Raising an exception on an empty socket is expensive here and it
  happens hundreds of times a second otherwise (+50 % frame rate).
* The bus is polled at least once a second even while frames stream in,
  and every 500 ms when idle. Without polling the panels stop applying
  frames after a few seconds (PROTOCOL.md §4.2).
"""
import time
import uselect
import network
import config
import panelbus
import ddp
import e131
import web

IDLE_MS = 300          # silence that counts as idle
KEEP_MS = 500          # poll interval when idle
MAX_NOPOLL_MS = 1000   # poll interval while frames stream in
RESCAN_MS = 5000       # minimum gap between re-enumerations


def wifi_connect(cfg, timeout=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        if not cfg['wifi']['ssid']:
            raise OSError('no Wi-Fi credentials: upload config.json')
        wlan.connect(cfg['wifi']['ssid'], cfg['wifi']['key'])
        t0 = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout * 1000:
                raise OSError('Wi-Fi: could not join %r' % cfg['wifi']['ssid'])
            time.sleep_ms(200)
    return wlan.ifconfig()[0]


def open_bus(cfg):
    bus = panelbus.PanelBus(tx=cfg['uart']['tx'], rx=cfg['uart']['rx'])
    bus.enumerate()
    bus.brightness(cfg['brightness'])
    return bus


def serve(cfg=None, verbose=True):
    cfg = cfg or config.load()
    ip = wifi_connect(cfg)
    bus = open_bus(cfg)
    d = ddp.DDPReceiver(bus, port=cfg['ddp_port'])
    e = e131.E131Receiver(bus, universe=cfg['e131_universe'], port=cfg['e131_port'],
                          per=cfg['e131_channels_per_panel'])
    w = web.WebServer(bus, port=cfg['http_port'], sources={'ddp': d, 'e131': e})
    if verbose:
        print('panels: %d%s' % (bus.npanels,
                                ' (layout string unstable)' if bus.layout_unstable else ''))
        print('DDP    udp://%s:%d' % (ip, cfg['ddp_port']))
        print('E1.31  udp://%s:%d  universe %d%s'
              % (ip, cfg['e131_port'], cfg['e131_universe'],
                 '  group %s' % e.group if e.group else ', unicast only'))
        print('HTTP   http://%s:%d/layout' % (ip, cfg['http_port']))

    poller = uselect.poll()
    for r in (d, e, w):
        poller.register(r.sock, uselect.POLLIN)
    handlers = {id(d.sock): d.tick, id(e.sock): e.tick, id(w.sock): w.tick}

    seen = d.frames + e.frames
    last_frame = last_keep = last_rescan = time.ticks_ms()
    while True:
        for sock, _ in poller.poll(5):
            h = handlers.get(id(sock))
            if h:
                h()
        now = time.ticks_ms()
        total = d.frames + e.frames
        if total != seen:
            seen = total
            last_frame = now
        if (time.ticks_diff(now, last_keep) >= MAX_NOPOLL_MS or
                (time.ticks_diff(now, last_frame) > IDLE_MS and
                 time.ticks_diff(now, last_keep) >= KEEP_MS)):
            r = bus.bulk_pull()
            last_keep = now
            # A moved panel silently stops applying frames until the bus is
            # enumerated again; the trailing 0xCC in the poll reply is the
            # signal (PROTOCOL.md §8). A short reply is not: far panels
            # legitimately miss the window now and then.
            if (r and panelbus.HOTSWAP in r and
                    time.ticks_diff(now, last_rescan) >= RESCAN_MS):
                n0 = bus.npanels
                try:
                    bus.enumerate()
                    bus.brightness(cfg['brightness'])
                except panelbus.BusError as err:
                    print('re-enumeration failed:', err)
                last_rescan = now
                if n0 != bus.npanels:
                    print('assembly changed: %d -> %d panels' % (n0, bus.npanels))
