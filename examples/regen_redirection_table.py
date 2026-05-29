#!/usr/bin/env python3
"""
Script to browse all videos and if external_ref is present, add it to a redirection CSV file:
external_ref, oid
"""
import argparse
import csv
import os
import sys


def regen_redirections_file(ngc):
    redir_count = 0
    videos = ngc.get_catalog(fmt='flat').get('videos', list())
    filename = 'redirections.csv'
    with open(filename, 'w') as f:
        writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_MINIMAL)
        for video in videos:
            external_ref = video.get('external_ref')
            if external_ref:
                writer.writerow([external_ref, video['oid']])
                redir_count += 1

    print(f'Wrote {redir_count} redirections to {filename}')


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
    ngc.check_server()

    regen_redirections_file(ngc)
