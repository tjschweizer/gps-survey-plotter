"""The application: state, editing rules and view payloads, with no GUI.

Everything a user can do lives here as plain Python. The web server in
`gpsrtk.web` is a thin layer that exposes it over HTTP, and the browser is a
view of it - so the behaviour can be tested without drawing anything.
"""

from .state import AppState

__all__ = ["AppState"]
