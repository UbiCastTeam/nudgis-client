#!/usr/bin/env python3
"""
Script to print annotations of a media into the standard output
in order to export it
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
    annotations = ngc.api('/annotations/list/', params={'oid': 'v125f52117974vspq8g1'})

    type_id = None
    annotation_type_name = 'comment'
    for val in annotations['types'].values():
        if val['slug'] == annotation_type_name:
            type_id = val['id']
    if type_id is None:
        print(f'Annotation type {annotation_type_name} not found')
        exit(1)

    for annotation in annotations['annotations']:
        # comment
        if annotation['type_id'] == type_id:
            s = '{poster} ({popularity} votes): {content}\n'.format(**annotation)
            print(s)
