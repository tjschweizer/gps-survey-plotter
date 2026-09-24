"""Entry point: `python -m gpsrtk [export.zip]`, the same as `yardsurvey`."""

from .web.server import main

if __name__ == "__main__":
    raise SystemExit(main())
