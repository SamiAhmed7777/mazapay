"""Treasury entrypoint: python3 -m mazapay.treasury.server

Kept deliberately thin (parent-owned): the HTTP layer lives in
mazapay/treasury/http.py and is imported lazily so this module stays
importable even while a lane is mid-flight.
"""
from . import settings


def main() -> None:  # pragma: no cover - manual run helper
    from .http import serve

    serve(settings.HOST, settings.PORT)


if __name__ == "__main__":  # pragma: no cover
    main()
