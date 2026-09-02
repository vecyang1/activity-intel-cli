"""Source adapters. Each builds URLs and parses bodies; none opens a socket.

``REGISTRY`` is the one ordered list of sources — the order output columns
follow, the order ``--sources`` choices are offered in. It lives here rather
than in ``cli`` so the renderer can read it without importing the command
layer, which imports the renderer: a registry in ``cli`` was the cycle that
kept 1,100 lines of rendering inside the entry point.
"""
from __future__ import annotations

from . import airbnb, klook, viator

REGISTRY = {klook.NAME: klook, airbnb.NAME: airbnb, viator.NAME: viator}

__all__ = ["REGISTRY", "airbnb", "klook", "viator"]
