#!/usr/bin/env python3
"""
Script to generate a CSV file for metadata from all media in the database
"""
import argparse
import os
import sys


def generate_csv(ngc, csv_path):
    with open(csv_path, 'wb') as f:
        print('Fetching catalog')
        catalog_csv = ngc.get_catalog(fmt='csv')
        print(f'Writing {csv_path}')
        f.write(catalog_csv.encode('utf8'))


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

    csv_path = f'media-{ngc.conf["SERVER_URL"].split("://")[1]}.csv'
    if os.path.isfile(csv_path):
        print(f'File {csv_path} already exists, exiting with error')
        sys.exit(1)

    generate_csv(ngc, csv_path)
    print(f'Finished writing {csv_path}')
