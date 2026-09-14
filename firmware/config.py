"""Board configuration.

Everything adjustable lives in config.json on the board's filesystem: copy
config.example.json, fill in Wi-Fi and pins, upload it as config.json next to
the modules. Missing keys fall back to the defaults below.
"""
import json

DEFAULTS = {
    'wifi': {'ssid': '', 'key': ''},
    'uart': {'tx': 17, 'rx': 16},
    'ddp_port': 4048,
    'e131_port': 5568,
    'e131_universe': 1,
    'e131_channels_per_panel': 3,
    'http_port': 80,
    'brightness': 255,
}


def load(path='config.json'):
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except OSError:
        pass
    out = dict(DEFAULTS)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = dict(out[k], **v)
        else:
            out[k] = v
    return out
