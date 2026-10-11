__version__ = "0.1.0"

# The backend runs without a console; every console-subsystem child it
# spawns (git, ffmpeg, scheduled-job helpers) must inherit
# CREATE_NO_WINDOW or the user sees cmd screens flash on each tick.
# Install once at package import so every entry point (server, workers,
# scheduler threads) is covered — explicit creationflags are honored.
from .procutil import install_hidden_children as _ihc

_ihc()
del _ihc
