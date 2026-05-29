#!/usr/bin/env python3
"""
Script to generate a JSON file for metadata from all media in the database. The primary nodes are channels.
"""
import argparse
import json
import os
import sys


def generate_json(ngc, json_path):
    with open(json_path, 'wb') as f:
        print('Fetching catalog as a JSON file')
        catalog_json = ngc.get_catalog(fmt='tree')
        print(f'Writing {json_path}')
        f.write(json.dumps(catalog_json).encode('utf-8'))


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

    json_path = f'media-{ngc.conf["SERVER_URL"].split("://")[1]}.json'
    if os.path.isfile(json_path):
        print(f'File {json_path} already exists, exiting with error')
        sys.exit(1)

    generate_json(ngc, json_path)
    print(f'Finished writing {json_path}')
