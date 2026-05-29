#!/usr/bin/env python3
"""
Script to monitor response time of a Nudgis portal.
"""
import argparse
from datetime import datetime
import os
import sys
import time

if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient import NudgisClient
    from nudgisclient.lib.utils import TTYColors as C

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
    while True:
        begin = datetime.now()
        before = time.time()
        print(f'{begin} ping')
        url = f'/?usage=mytest&ts={before}'
        print(ngc.api(url, timeout=2))
        took = int(1000 * (time.time() - before))
        color = C.RESET
        if took > 3000:
            color = C.RED
        elif took > 500:
            color = C.YELLOW
        print(f'{color}{url} took {took} ms{C.RESET}')
