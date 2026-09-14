"""Autostart: the board comes up as a network light receiver.

Listens for DDP and E1.31, serves the topology over HTTP. Animations live
outside the board, so changing an effect never means reflashing.

Stop:     Ctrl-C in the REPL.
Disable:  import os; os.remove('main.py')
"""
import time

print('main.py: starting the receiver in 2 s (Ctrl-C to stop)')
time.sleep(2)

try:
    import app
    app.serve()
except KeyboardInterrupt:
    print('stopped, REPL is yours')
except Exception as e:
    print('startup failed:', e)
