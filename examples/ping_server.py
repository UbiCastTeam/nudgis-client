#!/usr/bin/env python3
"""
Script to ping a Nudgis portal.
"""
import argparse
import os
import sys

if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient import NudgisClient

    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        'conf',
        default=None,
        help='The configuration to use.',
        nargs='?',
        type=str,
    )
    args = parser.parse_args()

    ngc = NudgisClient(args.conf)
    # ping
    print(ngc.api('/'))
