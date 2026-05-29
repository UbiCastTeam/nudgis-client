#!/usr/bin/env python3
"""
Example script that mass moves media into a channel based on a criteria (e.g. here a specific external_ref prefix)
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

    more = True
    start = ''
    index = 0

    external_ref_prefix = 'examplevalue'
    target_channel_oid = 'c12345678910'

    while more:
        print('//// Making request on latest (start=%s)' % start)
        response = ngc.api('latest/', params={'start': start, 'content': 'v', 'count': 20})
        for item in response['items']:
            oid = item['oid']
            index += 1
            print('// Media %s' % index)
            external_ref = ngc.api('medias/get/', params={'oid': oid, 'full': 'yes'})['info'].get('external_ref')
            if external_ref:
                if external_ref.startswith(external_ref_prefix) and item['parent_oid'] != target_channel_oid:
                    print(f'Moving {oid} into {target_channel_oid}')
                    ngc.api('medias/edit/', method='post', data={'oid': oid, 'channel': f'mscid-{target_channel_oid}'})
        start = response['max_date']
        more = response['more']
