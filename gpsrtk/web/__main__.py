"""`python -m gpsrtk.web [export.zip]` starts the browser UI."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
