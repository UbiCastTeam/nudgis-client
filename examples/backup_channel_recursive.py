#!/usr/bin/env python3
'''
Script to backup media from a channel and its sub channels of a Nudgis portal.

Backuped media are downloaded in a zip file for each media with metadata and best resource.
By default, files are placed in a directory named "backups" in the current directory.

Usage example:
python3 examples/backup_media_from_channel.py --conf conf.json --channel c126195118a0ahqtcr04
'''

import argparse
import os
import sys


def process_channel(ngc, channel_info, dir_path, backuped, failed, as_tree=False):
    # Browse channels from channel parent
    channel_items = ngc.api(
        'channels/content/',
        method='get',
        params=dict(parent_oid=channel_info['oid'], content='cvp')
    )

    # Check sub channels
    for item in channel_items.get('channels', []):
        print('Check videos in channel %s %s' % (item['oid'], item['title']))
        process_channel(ngc, item, dir_path, backuped, failed, as_tree)

    # Backup videos and photos
    items = channel_items.get('videos', []) + channel_items.get('photos_groups', [])
    for item in items:
        media_link = ngc.conf['SERVER_URL'] + '/permalink/' + item['oid'] + '/'
        print(f'// {C.PURPLE}{format_item(item)}{C.RESET} {media_link}')
        try:
            ngc.backup_media(item, dir_path, replicate_tree=as_tree)
        except Exception as err:
            print(f'{C.RED}{err}{C.RESET}')
            failed.append((item, str(err)))
        else:
            print(f'{C.GREEN}Backuped{C.RESET}')
            backuped.append(item)


def backup_media_from_channel(ngc, channel_oid, dir_path, as_tree=False):
    print('Starting backups...')

    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    backuped = list()
    failed = list()

    # Check if channel oid exists
    try:
        channel_parent = ngc.api('channels/get/', method='get', params=dict(oid=channel_oid))
    except Exception as e:
        print(
            'Please enter valid channel oid or check access permissions. '
            f'Error when trying to get channel was: {e}'
        )
        return 1

    process_channel(ngc, channel_parent['info'], dir_path, backuped, failed, as_tree)

    if backuped:
        print('%sMedia backuped successfully (%s):%s' % (C.GREEN, len(backuped), C.RESET))
        for item in backuped:
            print('  [%sOK%s] %s' % (C.GREEN, C.RESET, format_item(item)))
    if failed:
        print('%sMedia backups failed (%s):%s' % (C.RED, len(failed), C.RESET))
        for item, error in failed:
            print('  [%sKO%s] %s: %s' % (C.RED, C.RESET, format_item(item), error))
        print('%sSome media were not backuped.%s' % (C.YELLOW, C.RESET))
        return 1
    if backuped:
        print('%sAll media have been backuped successfully.%s' % (C.GREEN, C.RESET))
    else:
        print('No media to backup.')
    return 0


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nudgisclient import NudgisClient
    from nudgisclient.lib.utils import (
        format_item,
        TTYColors as C,
    )

    parser = argparse.ArgumentParser(description=__doc__.strip())

    parser.add_argument(
        '--conf',
        dest='configuration',
        help='Path to the configuration file.',
        required=True,
        type=str)
    parser.add_argument(
        '--directory',
        default='backups',
        dest='dir_path',
        help='Directory in which backuped media should be added.',
        type=str)
    parser.add_argument(
        '--tree',
        action='store_true',
        default=False,
        dest='as_tree',
        help='Place backuped media in sub directories depending on the channels path of the media.')
    parser.add_argument(
        '--channel',
        dest='channel_oid',
        help='Channel oid to check.',
        required=True,
        type=str)

    args = parser.parse_args()

    print('Configuration path: %s' % args.configuration)
    print('Backups directory: %s' % args.dir_path)
    print('Parent channel oid: %s' % args.channel_oid)

    # Check if configuration file exists
    if not args.configuration.startswith('unix:') and not os.path.exists(args.configuration):
        print('Invalid path for configuration file.')
        sys.exit(1)

    ngc = NudgisClient(args.configuration)
    ngc.check_server()
    # Increase default timeout because backups can be very disk intensive and slow the server
    ngc.conf['TIMEOUT'] = max(60, ngc.conf['TIMEOUT'])

    rc = backup_media_from_channel(ngc, args.channel_oid, args.dir_path, args.as_tree)
    sys.exit(rc)
