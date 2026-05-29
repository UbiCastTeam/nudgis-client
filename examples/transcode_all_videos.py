#!/usr/bin/env python3
"""
Script to transcode and clean all videos from a Nudgis portal.
"""
import argparse
import json
import os
import sys


def transcode_all_videos(ngc, purge):
    non_transcodable = failed = succeeded = 0

    videos = ngc.get_catalog(fmt='flat').get('videos', [])
    videos_count = len(videos)
    for index, item in enumerate(videos):
        print(f'// Media {index + 1}/{videos_count}: {item["oid"]}')
        try:
            transcoding_params = {'priority': 'low'}
            if purge:
                transcoding_params['behavior'] = 'delete'

            print(f'Starting transcoding task on {item["oid"]}')
            ngc.api(
                'tasks/start/',
                method='post',
                data=dict(
                    oid=item['oid'],
                    task='transcoding',
                    params=json.dumps(transcoding_params),
                ),
                timeout=300,
            )
        except ngc.RequestError as e:
            if 'has no usable ressources' in str(e):
                non_transcodable += 1
            else:
                print(f'WARNING: Failed to start transcoding task of video {item["oid"]}: {e}')
                failed += 1
        else:
            succeeded += 1
    print('%s transcoding tasks started.' % succeeded)
    print('%s transcoding tasks failed to be started.' % failed)
    print('%s media have no resouces and cannot be transcoded.' % non_transcodable)
    print('Total media count: %s.' % (succeeded + failed + non_transcodable))


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient import NudgisClient

    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        '--conf', help='Path to the configuration file.', required=True, type=str
    )
    parser.add_argument(
        '--purge',
        action='store_true',
        default=False,
        help='If set, will delete all existing resources; otherwise, only missing transcodings will be generated.',
    )
    args = parser.parse_args()

    ngc = NudgisClient(args.conf)
    ngc.check_server()
    transcode_all_videos(ngc, args.purge)
