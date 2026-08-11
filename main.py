#!/usr/bin/env python3
"""FoamViz -- browse OpenFOAM results in a web browser.

    python main.py --data ./data --port 8080
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from foamviz.app import FoamViz
from foamviz.case import find_cases


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

    cases = find_cases(args.data)
    if not cases:
        parser.error(
            f"no OpenFOAM case found under {args.data!r} "
            "(a case is a directory containing system/controlDict)"
        )
    print(f"FoamViz: {len(cases)} case(s) under {args.data}")
    for case in cases:
        print(f"  - {case.name}")

    app = FoamViz(args.data)
    app.start(port=args.port, host=args.host, open_browser=not args.server)


if __name__ == "__main__":
    main()
