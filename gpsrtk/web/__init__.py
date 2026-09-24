"""The browser UI: a local web server over `gpsrtk.app`, and its static page."""

from .server import create_app, main

__all__ = ["create_app", "main"]
