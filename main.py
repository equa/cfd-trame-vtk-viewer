#!/usr/bin/env python3
"""FoamViz -- browse OpenFOAM results in a web browser.

    python main.py --data ./data --port 8080
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from foamviz.app import FoamViz
from foamviz.case import find_cases

log = logging.getLogger("foamviz")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        default=str(Path(__file__).parent / "data"),
        help="directory holding OpenFOAM cases (or a single case directory)",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--server",
        action="store_true",
        help="do not open a browser; just serve",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Trame serves over aiohttp; surface its request/error logs too.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.WARNING)

    cases = find_cases(args.data)
    if not cases:
        msg = (
            f"no OpenFOAM case found under {args.data!r} "
            "(a case is a directory containing system/controlDict)"
        )
        # Interactive use: a mistyped --data is worth failing on. Service mode
        # (--server) keeps running and discovers cases on demand as they appear.
        if not args.server:
            parser.error(msg)
        log.warning("%s; serving empty, will pick up cases on demand", msg)
    else:
        log.info(
            "%d case(s) under %s: %s",
            len(cases), args.data, ", ".join(c.name for c in cases),
        )

    log.info("serving on http://%s:%d/  (open_browser=%s)",
             args.host, args.port, not args.server)
    app = FoamViz(args.data)
    app.start(port=args.port, host=args.host, open_browser=not args.server)


if __name__ == "__main__":
    main()
