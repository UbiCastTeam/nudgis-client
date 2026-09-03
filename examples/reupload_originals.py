#!/usr/bin/env python3
"""
Script to replace all the resource files of a batch of media by a new video file.
The new video files must be playable mp4 files.

For each video file of ``--folder``, the script looks up the media object id in the
``--csv`` mapping file (the file name without its extension must match the first column),
uploads the file as a new resource of that media and deletes all the other resource files
of the media.

The mapping file must be a CSV file with a header and at least the ``source_id`` and
``oid`` columns, for example::

    source_id,oid
    00077418-e671-4c40-bce2-934d778cbe9f,v126d5681a914tzllpi6
    001ed551-19ad-49f9-8a85-d7dfc45fecf9,v126d5681a91a42ix2mn

The steps for each file are:

1. resolve the object id using the mapping file and check that the media exists;
2. get the upload target of the media (``tasks/get-upload-config``);
3. upload the file in that target (``upload`` and ``upload/complete``);
4. refresh the resources of the media (``medias/resources-check``);
5. list the resources (``medias/resources-list``) and identify the uploaded file, which
   may have been renamed with an ``_original`` or a ``_clean`` particle;
6. delete all the other resource files of the media (``medias/resources-delete``);
7. remove the ``_original`` or ``_clean`` particle if any (``medias/resources-rename``).

Nothing is ever deleted if the uploaded file cannot be found in the resources of the media,
so a failed upload cannot leave a media without any resource. The deletion is done before
the renaming to make sure that the name of the uploaded file is not already taken by one of
the resources to delete.

Without ``--apply``, the script only performs the read-only calls and reports what would be
uploaded and deleted. The result of each media is written in ``--result-file`` so that a run
interrupted in the middle of the batch can be resumed with ``--resume``.

Examples:

    ./examples/reupload_originals.py --conf myconf.json --folder ./originals --csv ./mapping.csv
    ./examples/reupload_originals.py --conf myconf.json --folder ./originals --csv ./mapping.csv --apply
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import date
import logging
from pathlib import Path
import re
import sys
from typing import Callable

try:
    from nudgisclient import NudgisClient, NudgisRequestError
    from nudgisclient.lib.utils import configure_logging, format_bytes
except ModuleNotFoundError:  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from nudgisclient import NudgisClient, NudgisRequestError
    from nudgisclient.lib.utils import configure_logging, format_bytes


logger = logging.getLogger(__name__)

# Extensions of the files to upload (lower case, with leading dot).
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.mp3', '.m4a'}
# Particles that Nudgis can add to the name of an uploaded file.
NAME_PARTICLES = ('_original', '_clean')
# Only the resources hosted by the portal can be renamed or deleted.
MANAGED_SERVICES = ('local', 'object')
# Formats that are not files and that cannot be deleted with "medias/resources-delete".
UNMANAGED_FORMATS = ('embed', 'youtube')
# Expected format of an upload target code.
TARGET_CODE_RE = re.compile(r'^[A-Za-z0-9_-]{10,60}$')
# Columns of the mapping file and of the result file.
MAPPING_FIELDS = ('source_id', 'oid')
RESULT_FIELDS = ('source_id', 'oid', 'status', 'detail')


@dataclass
class Summary:
    """Counters and messages of the whole run."""

    done: int = 0
    skipped: int = 0
    failed: int = 0
    uploaded_size: int = 0
    deleted_count: int = 0
    deleted_size: int = 0
    unmapped: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warning(self, message: str, *args) -> None:
        logger.warning(message, *args)
        self.warnings.append(message % args if args else message)


def read_mapping(csv_path: Path) -> dict[str, str]:
    """
    Read the "source_id" -> "oid" mapping file.
    The entries that are incomplete or that would make the mapping ambiguous are dropped.
    """
    mapping: dict[str, str] = {}
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as fo:
        reader = csv.DictReader(fo, skipinitialspace=True)
        missing_fields = [name for name in MAPPING_FIELDS if name not in (reader.fieldnames or [])]
        if missing_fields:
            raise ValueError(
                f'The mapping file "{csv_path}" has no "{", ".join(missing_fields)}" column '
                f'(columns found: {", ".join(reader.fieldnames or ["none"])}).'
            )

        for index, row in enumerate(reader, start=2):
            source_id = (row.get('source_id') or '').strip()
            oid = (row.get('oid') or '').strip()
            if not source_id or not oid:
                logger.warning('Line %s of the mapping file is incomplete, entry ignored.', index)
                continue
            if source_id in mapping:
                logger.warning(
                    'The source id "%s" is present more than once in the mapping file, '
                    'the entry of line %s is ignored.',
                    source_id, index,
                )
                continue
            mapping[source_id] = oid

    # Several files sent to the same media would delete each other, so the ambiguous entries are dropped
    oids_count: dict[str, int] = {}
    for oid in mapping.values():
        oids_count[oid] = oids_count.get(oid, 0) + 1
    duplicated = {oid for oid, count in oids_count.items() if count > 1}
    for oid in sorted(duplicated):
        source_ids = sorted(sid for sid, mapped in mapping.items() if mapped == oid)
        logger.warning(
            'The media "%s" is targeted by several source ids (%s), all these entries are ignored.',
            oid, ', '.join(source_ids),
        )
        for source_id in source_ids:
            del mapping[source_id]

    logger.info('%s usable entries found in the mapping file "%s".', len(mapping), csv_path)
    return mapping


def read_done_source_ids(result_path: Path) -> set[str]:
    """Get the source ids already processed successfully in a previous run."""
    if not result_path.is_file():
        return set()
    with open(result_path, 'r', encoding='utf-8', newline='') as fo:
        return {
            (row.get('source_id') or '').strip()
            for row in csv.DictReader(fo)
            if (row.get('status') or '').strip() == 'done'
        }


def list_video_files(folder: Path) -> list[Path]:
    """Get the video files to upload, sorted by name (the folder is not scanned recursively)."""
    files = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            logger.warning('The file "%s" is not a video file, it will not be uploaded.', path.name)
            continue
        files.append(path)
    return files


def is_managed(resource: dict) -> bool:
    """Tell if a resource is a file hosted by the portal and that can be renamed or deleted."""
    return (
        (resource.get('manager') or {}).get('service') in MANAGED_SERVICES
        and resource.get('format') not in UNMANAGED_FORMATS
    )


def list_resources(ngc: NudgisClient, oid: str) -> list[dict]:
    """Get the resources of a media."""
    return ngc.api('medias/resources-list/', params=dict(oid=oid))['resources']


def get_upload_target(ngc: NudgisClient, oid: str) -> str:
    """Get the code of the directory in which the resources of a media must be uploaded."""
    config = ngc.api('tasks/get-upload-config/', params=dict(oid=oid))['config']
    code = (config.get('code') or '').strip()
    if not TARGET_CODE_RE.match(code):
        raise ValueError(f'The upload configuration of the media does not contain a usable code: {config}.')
    return code


def get_sharing(ngc: NudgisClient, oid: str) -> list[str]:
    """Get the object ids of the other media using the same resource files as the given media."""
    sharing = ngc.api('medias/resources-info/', params=dict(oid=oid)).get('sharing') or []
    # The list items are expected to be media objects, but a list of object ids is also handled
    other_oids = [item.get('oid') if isinstance(item, dict) else item for item in sharing]
    return [item for item in other_oids if item and item != oid]


def make_progress_logger(label: str) -> Callable[[float], None]:
    """Build an upload progress callback that logs the progress every 10%."""
    state = {'logged': 0.}

    def log_progress(progress: float) -> None:
        if progress >= state['logged']:
            state['logged'] = progress + 0.1
            logger.info('%s: upload progress is %.0f%%.', label, 100 * progress)

    return log_progress


def get_expected_names(file_name: str) -> list[str]:
    """
    Get the possible names of an uploaded file in the resources of a media.
    Nudgis can add a particle to the name of the file when it is detected as an original
    or as a cleaned version of the video.
    """
    stem, _, suffix = file_name.rpartition('.')
    return [file_name] + [f'{stem}{particle}.{suffix}' for particle in NAME_PARTICLES]


def find_uploaded_resource(
    resources: list[dict], file_name: str, known_paths: set[str] | None = None
) -> dict | None:
    """
    Get the resource matching the uploaded file, if it is already known by the portal.
    ``known_paths`` must contain the paths of the resources that existed before the upload:
    a resource that was not there before is always the uploaded file, while a resource that
    was already there can only be the uploaded file if it has the exact name of the file
    (in which case the upload has overwritten it).
    """
    known_paths = known_paths or set()
    by_path = {resource['path']: resource for resource in resources}
    for name in get_expected_names(file_name):
        if name in by_path and name not in known_paths:
            return by_path[name]
    return by_path.get(file_name)


def delete_resources(ngc: NudgisClient, oid: str, resources: list[dict], summary: Summary) -> None:
    """Delete the given resources of a media, one by one to keep the requests short."""
    for resource in resources:
        try:
            ngc.api(
                'medias/resources-delete/',
                method='post',
                data=dict(oid=oid, names=resource['path']),
                timeout=180,
            )
        except NudgisRequestError as err:
            if 'read timeout=' in str(err):
                # The deletion is very disk intensive and can be longer than the timeout
                summary.warning(
                    'Media %s: The deletion request of "%s" timed out, the file may have been deleted anyway.',
                    oid, resource['path'],
                )
            else:
                summary.warning(
                    'Media %s: Failed to delete the resource "%s": %s', oid, resource['path'], str(err).strip()
                )
            continue
        logger.info('Media %s: Resource "%s" deleted.', oid, resource['path'])
        summary.deleted_count += 1
        summary.deleted_size += resource.get('file_size') or 0


def process_media(
    ngc: NudgisClient, video_path: Path, oid: str, summary: Summary, apply: bool
) -> tuple[str, str]:
    """
    Replace all the resources of the media by the given video file.
    Return the status ("done", "planned", "skipped" or "failed") and a message for the result file.
    """
    media = ngc.api('medias/get/', params=dict(oid=oid))['info']
    logger.info('Media %s "%s": Processing file "%s".', oid, media['title'], video_path.name)

    if media.get('trash_data'):
        return 'skipped', 'The media is in the recycle bin.'

    resources_before = list_resources(ngc, oid)
    to_delete = []
    for resource in resources_before:
        if is_managed(resource):
            to_delete.append(resource)
        else:
            summary.warning(
                'Media %s: The resource "%s" is not hosted by the portal and will not be deleted.',
                oid, resource['path'] or resource['format'],
            )

    # A media sharing its resources with other media cannot be processed without breaking them
    shared_with = get_sharing(ngc, oid)
    if shared_with:
        return 'skipped', f'The resources are shared with other media ({", ".join(shared_with)}).'

    # This read only call is also made in dry run mode to check that the target can be resolved
    target_code = get_upload_target(ngc, oid)
    file_size = video_path.stat().st_size

    if not apply:
        logger.info(
            'Media %s: [Dry run] The file "%s" (%s) would be uploaded in "%s" '
            'and %s resource(s) would be deleted (%s).',
            oid, video_path.name, format_bytes(file_size), target_code, len(to_delete),
            ', '.join(resource['path'] for resource in to_delete) or 'none',
        )
        summary.uploaded_size += file_size
        summary.deleted_count += len(to_delete)
        summary.deleted_size += sum(resource.get('file_size') or 0 for resource in to_delete)
        return 'planned', f'{len(to_delete)} resource(s) to delete.'

    ngc.chunked_upload(
        file_path=video_path,
        remote_path=f'{target_code}/{video_path.name}',
        progress_callback=make_progress_logger(f'Media {oid}'),
    )
    summary.uploaded_size += file_size

    # Make the portal discover the uploaded file
    ngc.api('medias/resources-check/', method='post', data=dict(oid=oid), timeout=180)
    resources = list_resources(ngc, oid)
    known_paths = {resource['path'] for resource in resources_before}
    uploaded = find_uploaded_resource(resources, video_path.name, known_paths)
    if uploaded is None:
        return 'failed', (
            'The uploaded file has not been added in the resources of the media '
            f'(expected one of: {", ".join(get_expected_names(video_path.name))}).'
        )
    logger.info('Media %s: The uploaded file has been added as the resource "%s".', oid, uploaded['path'])

    # Delete all the resources except the uploaded one. The deletion is done before the renaming
    # to free the target name and to handle the leftovers of a previous interrupted run.
    to_delete = [
        resource for resource in resources
        if is_managed(resource) and resource['path'] != uploaded['path']
    ]
    delete_resources(ngc, oid, to_delete, summary)

    # Remove the particle added by the portal, if any
    if uploaded['path'] != video_path.name:
        try:
            ngc.api(
                'medias/resources-rename/',
                method='post',
                data=dict(oid=oid, name=uploaded['path'], new_name=video_path.name),
            )
        except NudgisRequestError as err:
            summary.warning(
                'Media %s: Failed to rename the resource "%s" into "%s": %s',
                oid, uploaded['path'], video_path.name, str(err).strip(),
            )
        else:
            logger.info(
                'Media %s: Resource "%s" renamed into "%s".', oid, uploaded['path'], video_path.name
            )

    # Refresh the resources a last time so that the media does not reference deleted files
    ngc.api('medias/resources-check/', method='post', data=dict(oid=oid), timeout=180)
    return 'done', f'{len(to_delete)} resource(s) deleted.'


def reupload_files(
    ngc: NudgisClient,
    video_files: list[Path],
    mapping: dict[str, str],
    result_path: Path,
    done_source_ids: set[str],
    apply: bool,
) -> Summary:
    """Process all the given video files and write the result of each one in the result file."""
    summary = Summary()
    write_header = not result_path.is_file() or not result_path.stat().st_size
    with open(result_path, 'a', encoding='utf-8', newline='') as fo:
        writer = csv.DictWriter(fo, fieldnames=RESULT_FIELDS)
        if write_header:
            writer.writeheader()

        for index, video_path in enumerate(video_files, start=1):
            source_id = video_path.stem
            oid = mapping.get(source_id)
            logger.info('-- Media %s/%s: source id "%s".', index, len(video_files), source_id)

            if not oid:
                summary.warning('No object id found in the mapping file for "%s", file ignored.', video_path.name)
                summary.unmapped.append(source_id)
                summary.skipped += 1
                continue
            if source_id in done_source_ids:
                logger.info('The source id "%s" has already been processed, file ignored.', source_id)
                summary.skipped += 1
                continue

            try:
                status, detail = process_media(ngc, video_path, oid, summary, apply)
            except (NudgisRequestError, OSError, ValueError, KeyError) as err:
                status, detail = 'failed', f'{type(err).__name__}: {str(err).strip()}'

            if status == 'failed':
                logger.error('Media %s: Processing of "%s" failed: %s', oid, video_path.name, detail)
                summary.failed += 1
            elif status == 'skipped':
                summary.warning('Media %s: File "%s" ignored: %s', oid, video_path.name, detail)
                summary.skipped += 1
            else:
                logger.info('Media %s: File "%s" processed: %s', oid, video_path.name, detail)
                summary.done += 1

            writer.writerow(dict(source_id=source_id, oid=oid, status=status, detail=detail))
            fo.flush()

    processed_source_ids = {path.stem for path in video_files}
    summary.missing_files = sorted(set(mapping) - processed_source_ids)
    return summary


def print_summary(summary: Summary, apply: bool) -> None:
    """Log the final report of the run."""
    logger.info('Report:')
    logger.info('  Media %s: %s', 'processed' if apply else 'to process', summary.done)
    logger.info('  Media ignored: %s', summary.skipped)
    logger.info('  Media in error: %s', summary.failed)
    logger.info(
        '  Files %s: %s', 'uploaded' if apply else 'to upload', format_bytes(summary.uploaded_size)
    )
    logger.info(
        '  Resources %s: %s (%s)',
        'deleted' if apply else 'to delete',
        summary.deleted_count,
        format_bytes(summary.deleted_size),
    )
    if summary.unmapped:
        logger.info('  Files without object id: %s (%s)', len(summary.unmapped), ', '.join(summary.unmapped))
    if summary.missing_files:
        logger.info(
            '  Mapping entries without file in the folder: %s (%s)',
            len(summary.missing_files), ', '.join(summary.missing_files),
        )
    if summary.warnings:
        logger.info('  Warnings: %s', len(summary.warnings))
    if not apply:
        logger.info('This was a dry run, re-run with "--apply" to upload and delete the files.')


def reupload_originals(sys_args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        'reupload_originals',
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        help='Path to the configuration file (e.g. myconfig.json).',
        required=True,
    )
    parser.add_argument(
        '--folder',
        help='Path to the directory containing the video files to upload (not scanned recursively).',
        required=True,
        type=Path,
    )
    parser.add_argument(
        '--csv',
        dest='csv_path',
        help='Path to the CSV file mapping the name of each file (without its extension) to a media object id.',
        required=True,
        type=Path,
    )
    parser.add_argument(
        '--apply',
        help='Actually upload the files and delete the existing resources. Without this flag, the script '
             'only checks the files and reports what would be uploaded and deleted.',
        action='store_true',
    )
    parser.add_argument(
        '--result-file',
        help='Path of the CSV file in which the result of each media is written (appended if it exists).',
        default=Path(f'./reupload_result_{date.today().strftime("%Y-%m-%d")}.csv'),
        type=Path,
    )
    parser.add_argument(
        '--resume',
        help='Ignore the files already processed successfully according to the result file.',
        action='store_true',
    )
    parser.add_argument(
        '--limit',
        help='Maximum number of files to process. Useful to check the result on a few media first.',
        default=0,
        type=int,
    )
    parser.add_argument(
        '--log-level',
        help='Log level.',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug'],
    )
    args = parser.parse_args(sys_args)

    configure_logging(args.log_level.upper())

    if not args.folder.is_dir():
        logger.error('The videos folder "%s" does not exist.', args.folder)
        return 1
    if not args.csv_path.is_file():
        logger.error('The mapping file "%s" does not exist.', args.csv_path)
        return 1

    try:
        mapping = read_mapping(args.csv_path)
    except (OSError, ValueError, csv.Error) as err:
        logger.error('Failed to read the mapping file: %s', err)
        return 1
    if not mapping:
        logger.error('The mapping file "%s" contains no usable entry.', args.csv_path)
        return 1

    video_files = list_video_files(args.folder)
    if not video_files:
        logger.error('No video file found in the folder "%s".', args.folder)
        return 1
    if args.limit > 0:
        video_files = video_files[:args.limit]
        logger.info('Only the first %s video file(s) will be processed.', len(video_files))

    done_source_ids = read_done_source_ids(args.result_file) if args.resume else set()
    if done_source_ids:
        logger.info(
            '%s source id(s) already processed according to "%s" will be ignored.',
            len(done_source_ids), args.result_file,
        )

    ngc = NudgisClient(args.conf, setup_logging=False)
    # The uploads and the deletions are slow, the default timeout is far too short for them
    ngc.conf['TIMEOUT'] = max(600, ngc.conf['TIMEOUT'])
    try:
        ngc.check_server()
    except NudgisRequestError as err:
        logger.error('Cannot reach the Nudgis server: %s', err)
        return 1

    summary = reupload_files(
        ngc, video_files, mapping, args.result_file, done_source_ids, args.apply
    )
    print_summary(summary, args.apply)
    logger.info('The result of each media has been written in "%s".', args.result_file)
    return 1 if summary.failed else 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(reupload_originals(sys.argv[1:]))
