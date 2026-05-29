#!/usr/bin/env python3
"""
Script to delete unwanted video qualities from a channel or using a csv file.
All best mp4 or mp3 files are preserved.
"""

import argparse
import csv
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import sys
import time

try:
    from nudgisclient import NudgisClient, NudgisRequestError
except ModuleNotFoundError:
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from nudgisclient import NudgisClient, NudgisRequestError
from nudgisclient.lib.utils import format_bytes

logger = logging.getLogger(__name__)


def remove_resources(
    ngc, video_item, qualities_to_delete, formats_to_delete,
    older_than_days=0, only_from_miris=False, enable_delete=False
):
    video_repr = f'Video {video_item["oid"]} "{video_item["title"]}"'

    if only_from_miris and 'miris-box-' not in video_item['origin']:
        logger.info(
            '%s does not originate from a Miris recorder, media skipped.', video_repr
        )
        return

    if older_than_days > 0:
        media_date = datetime.strptime(video_item['add_date'], '%Y-%m-%d %H:%M:%S')
        if media_date > datetime.now() - timedelta(days=older_than_days):
            # Newer than cutoff, skip
            logger.info('%s is newer than %s days ago, media skipped.', video_repr, older_than_days)
            return

    if video_item.get('trash_data'):
        logger.info('%s is in the recycle bin, skipping it.', video_repr)
        return

    # Update resources from video media
    resources = ngc.api('medias/resources-check/', method='post', data=dict(oid=video_item['oid']))

    # Get resources from video media
    resources = ngc.api('medias/resources-list/', params=dict(oid=video_item['oid']))['resources']

    # Ignore non managed resources
    resources = [res for res in resources if (
        (res.get('manager') or {}).get('service') in ('local', 'object')
        and res['format'] not in ('embed', 'youtube')
    )]

    if len(resources) <= 1:
        logger.info('%s has only one or zero resource, nothing to delete.', video_repr)
        return

    # Get reference format depending on media qualities
    ref_format = 'mp3'
    for res in resources:
        if res['height'] > 0:
            ref_format = 'mp4'
            break

    # Sort by format and decreasing quality
    resources.sort(key=lambda a: (
        a['format'] != ref_format,
        a['format'] == 'm3u8',
        '_clean.' in a['path'] or '_original.' in a['path'],
        -a['height'],
        -a['file_size']
    ))

    # Always keep the reference or the best resource but never a m3u8
    ref_res = resources.pop(0)
    logger.info('%s: Reference file is "%s" and will not be deleted.', video_repr, ref_res['path'])
    if ref_res['path'].endswith('.m3u8'):
        logger.info('%s: Warning: The reference is a m3u8 file, media skipped.', video_repr)
        return

    # Filter resources
    if formats_to_delete:
        filtered_resources = [res for res in resources if res['format'] in formats_to_delete]
    else:
        filtered_resources = resources

    if not filtered_resources:
        logger.info('%s has no matching resources to delete.', video_repr)
        return

    del_count = 0
    del_size = 0
    for res in filtered_resources:
        if qualities_to_delete == '*' or res['height'] in qualities_to_delete:
            if enable_delete:
                try:
                    ngc.api(
                        'medias/resources-delete/',
                        method='post',
                        data=dict(oid=video_item['oid'], names=res['path']),
                        timeout=180
                    )
                    del_count += 1
                    del_size += res['file_size']
                except NudgisRequestError as err:
                    if 'read timeout=' in str(err):
                        logger.warning(
                            '%s: The deletion request timed out for "%s", this error can be ignored.',
                            video_repr, res['path']
                        )
                    else:
                        logger.error(
                            '%s: Failed to delete resource "%s": %s', video_repr, res['path'], str(err).strip()
                        )
                else:
                    logger.info(
                        '%s: Resource "%s" has been deleted successully.', video_repr, res['path']
                    )
            else:
                logger.info('[Dry Run] %s: Resource "%s" would be deleted.', video_repr, res['path'])
                del_count += 1
                del_size += res['file_size']
    if not del_count:
        logger.info('%s: Nothing to delete in this media.', video_repr)

    return del_count, del_size


def iter_channel_videos(ngc, channel_item):
    logger.info('Processing channel %s "%s".', channel_item['oid'], channel_item['title'])
    # Get channel content
    channel_items = ngc.api(
        'channels/content/',
        method='get',
        params=dict(parent_oid=channel_item['oid'], content='cv')
    )

    # Check sub channels
    for sub_item in channel_items.get('channels', []):
        if sub_item['oid'] == 'c00000000000000trash':
            # Ignore recycle bin channel
            continue
        yield from iter_channel_videos(ngc, sub_item)

    # Get video informations
    for video_item in channel_items.get('videos', []):
        yield video_item


def iter_csv_file_videos(ngc, csv_file):
    with open(csv_file, 'r') as csvfile:
        csvreader = csv.reader(csvfile, skipinitialspace=True)

        # Skip header
        next(csvreader)

        for row in csvreader:
            # First column must be the oid
            video_oid = row[0]

            try:
                # Get media title and check it exists
                video_item = ngc.api('medias/get/', params=dict(oid=video_oid))['info']
            except NudgisRequestError as err:
                logger.info('-- Media %s ignored:', video_oid)
                logger.info('Failed to get title of media: %s', str(err).strip())
            else:
                yield video_item


def iter_videos(ngc, channel_oid, csv_file):
    if csv_file:
        yield from iter_csv_file_videos(ngc, csv_file)
    elif channel_oid:
        try:
            channel_item = ngc.api('channels/get/', method='get', params=dict(oid=channel_oid))['info']
        except NudgisRequestError as err:
            logger.info('Please enter a valid channel oid or check access permissions.')
            logger.info('Error when trying to get channel was: %s', str(err).strip())
            raise
        yield from iter_channel_videos(ngc, channel_item)
    else:
        channel_item = {'oid': '', 'title': 'root'}
        yield from iter_channel_videos(ngc, channel_item)


def delete_qualities_from_videos(
    ngc, channel_oid, csv_file, qualities_to_delete, formats_to_delete, older_than_days,
    only_from_miris, enable_delete=False
):
    deleted = {'count': 0, 'size': 0}
    for video_item in iter_videos(ngc, channel_oid, csv_file):
        params = dict(
            ngc=ngc,
            video_item=video_item,
            qualities_to_delete=qualities_to_delete,
            formats_to_delete=formats_to_delete,
            older_than_days=older_than_days,
            only_from_miris=only_from_miris,
            enable_delete=enable_delete,
        )
        try:
            result = remove_resources(**params)
        except Exception as err:
            logger.warning('Error: %s, retrying in 30s.', err)
            time.sleep(30)
            result = remove_resources(**params)

        if result is not None:
            deleted['count'] += result[0]
            deleted['size'] += result[1]

    if enable_delete:
        logger.info(
            'Deleted %s (%s resources).', format_bytes(deleted['size']), deleted['count']
        )
    else:
        logger.info(
            'Would have freed %s (%s resources) if run with "--delete".',
            format_bytes(deleted['size']), deleted['count']
        )
    return 0


def qualities_type(value):
    if value == '*':
        return value
    if not re.match(r'(\d+,?)+', value):
        raise ValueError('Invalid format for "qualities", expected format is "height1,height2", for example "360,720".')
    qualities = [int(v) for v in value.strip(',').split(',')]
    return qualities


def formats_type(value):
    value = value.strip('\t ,')
    return value.split(',') if value else []


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        '--conf',
        dest='configuration',
        help='Path to the configuration file.',
        required=True,
        type=str
    )
    parser.add_argument(
        '--qualities',
        dest='qualities',
        help='Qualities to delete. Format is "height1,height2", for example "360,720". '
             'Use "0" to target audio files. Use "*" to delete all qualities. '
             'The reference mp4 or mp3 file is never deleted.',
        required=True,
        type=qualities_type
    )
    parser.add_argument(
        '--formats',
        dest='formats',
        help='File extensions to delete. Format is for example "mp4,aspx". '
             'If no value is specified, all formats will be targetted. '
             'The reference mp4 or mp3 file is never deleted.',
        default=[],
        type=formats_type
    )
    parser.add_argument(
        '--older-than-days',
        dest='older_than_days',
        help='Process only media older than this number of days.',
        default=0,
        required=False,
        type=int
    )
    parser.add_argument(
        '--only-from-miris',
        action='store_true',
        default=False,
        dest='only_from_miris',
        help='Only process media originating from Miris recorders.'
    )
    parser.add_argument(
        '--delete',
        action='store_true',
        default=False,
        dest='enable_delete',
        help='Enable files deletion. If not enabled, the script will be run in dry run mode.'
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--channel',
        dest='channel_oid',
        help='Channel oid to check. If no channel and no CSV is given, all channels will be checked.',
        type=str
    )
    group.add_argument(
        '--csv',
        dest='csv_file',
        help='CSV file with a list of videos oids. If no channel and no CSV is given, all channels will be checked.',
        type=str
    )
    args = parser.parse_args()

    print(f'Configuration path: {args.configuration}')
    print(f'Qualities to delete: {args.qualities}')
    print(f'Formats to delete: {args.formats or "any"}')
    print(f'Skip media newer than (days): {args.older_than_days}')
    print(f'Only process media originating from Miris recorders: {args.only_from_miris}')
    print(f'Enable delete: {args.enable_delete}')
    print(f'Parent channel oid: {args.channel_oid}')
    print(f'CSV file: {args.csv_file}')

    # Check if configuration file exists
    if not args.configuration.startswith('unix:') and not Path(args.configuration).exists():
        print('Invalid path for configuration file.')
        return 1

    ngc = NudgisClient(args.configuration)
    ngc.check_server()
    # Increase default timeout because deletions can be very disk intensive and slow the server
    ngc.conf['TIMEOUT'] = max(60, ngc.conf['TIMEOUT'])

    rc = delete_qualities_from_videos(
        ngc,
        args.channel_oid,
        args.csv_file,
        args.qualities,
        args.formats,
        args.older_than_days,
        args.only_from_miris,
        args.enable_delete
    )
    return rc


if __name__ == '__main__':
    sys.exit(main())
