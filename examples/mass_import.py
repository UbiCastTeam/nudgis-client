#!/usr/bin/env python3
"""
Mass media import script for Nudgis migrations.

This script implements the "Nudgis Standard Migration"
(see https://docs.google.com/document/d/1ZzrQJ_50vnyeotfXd-0dgHO5I68LhAuUlVsvjCsNKy0/edit).
It scans a source directory laid out according to the migration standard and either:

- audits it (default behaviour, no ``--apply``): it checks that the file tree is
  correctly structured, that the media files are valid (using ``ffprobe``), that the
  ``metadata.csv`` and ``annotations.csv`` files are well formed and consistent with the
  files on disk, and it runs a few server-side checks (slug uniqueness, annotation types,
  speaker resolution, explicit channels);
- imports it (with ``--apply``): it uploads each media (reusing the folder tree as a
  channel tree), attaches subtitles and additional audio tracks, applies the metadata and
  the annotations, and writes a ``source_id`` -> ``oid`` mapping file.

Multi-stream (side-by-side) media are combined into a single video with ``ffmpeg`` during
the import (see ``compose_multistream.py``); the temporary combined file is written under
``--temp-dir`` and removed once the upload succeeds.

Example:

    ./examples/mass_import.py --conf myconf.json --source-dir ./migration --channel "Migration 2026"
    ./examples/mass_import.py --conf myconf.json --source-dir ./migration --channel "Migration 2026" --apply
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
import json
import logging
from pathlib import Path
import re
import subprocess
import sys

try:
    from examples.compose_multistream import compose_streams
    from nudgisclient import NudgisClient, NudgisRequestError
    from nudgisclient.lib.utils import configure_logging
except ModuleNotFoundError:  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from examples.compose_multistream import compose_streams
    from nudgisclient import NudgisClient, NudgisRequestError
    from nudgisclient.lib.utils import configure_logging


logger = logging.getLogger(__name__)

# Recognized file extensions (lower case, with leading dot).
VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.wmv', '.flv',
}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus'}
SUBTITLE_EXTENSIONS = {'.srt', '.vtt'}

# Name of the metadata file (expected at the root of the source directory).
METADATA_FILENAME = 'metadata.csv'
# Name of the annotations directory and file (expected at the root of the source directory).
ANNOTATIONS_DIRNAME = 'annotations'
ANNOTATIONS_FILENAME = 'annotations.csv'

# Columns of metadata.csv. The mandatory ones are flagged with "*" in the specifications.
METADATA_MANDATORY_FIELDS = {'source_id', 'title'}
METADATA_KNOWN_FIELDS = {
    'source_id', 'title', 'slug', 'description', 'keywords', 'categories', 'language',
    'creation', 'speaker_name', 'speaker_email', 'company_name', 'company_url',
    'license_name', 'license_url', 'channel', 'validated', 'unlisted', 'detect_slides',
}
# Columns of annotations.csv.
ANNOTATIONS_MANDATORY_FIELDS = {'source_id', 'type'}
ANNOTATIONS_KNOWN_FIELDS = {
    'source_id', 'type', 'time', 'title', 'content', 'keywords', 'attachment',
}
# Annotation type slugs available by default on Nudgis.
INTERNAL_ANNOTATION_TYPES = {'chapter', 'slide', 'attachment', 'activity'}

# Allowed values for the yes/no metadata fields.
YESNO_VALUES = {'yes', 'no'}

# Validation patterns.
SLUG_RE = re.compile(r'^[a-z0-9_-]+$')
# Language code pattern (ISO 639-2).
LANGUAGE_RE = re.compile(r'^[a-z]{3}$')
# A "_stream<digit>" suffix designates a secondary stream of a multi-stream media.
STREAM_SUFFIX_RE = re.compile(r'^(.+)_stream(\d+)$')
# A "_<3 letters>" suffix designates a linked subtitle or audio track.
LANG_SUFFIX_RE = re.compile(r'^(.+)_([a-z]{3})$')
# Single field max length.
MAX_FIELD_LENGTH = 250
# Maximum number of streams that can be combined into a multi-stream media.
MAX_STREAMS = 6


@dataclass
class MediaGroup:
    """
    All the data that makes up a single media, grouped by their common source id.

    Besides the files found on disk, a group also carries its validated ``metadata.csv``
    row (``metadata``) and its ``annotations.csv`` rows (``annotations``) so that everything
    related to a media is accessible from a single object.
    """

    source_id: str
    object_id: str | None
    main_file: Path
    rel_dir: Path
    subtitles: dict[str, Path] = field(default_factory=dict)
    audio_tracks: dict[str, Path] = field(default_factory=dict)
    extra_streams: dict[int, Path] = field(default_factory=dict)
    metadata: dict[str, str] | None = None
    annotations: list[dict[str, str]] = field(default_factory=list)
    existing_elements: list[str] = field(default_factory=list)

    @property
    def external_ref(self) -> str:
        return f'migration:{self.source_id}'


@dataclass
class Report:
    """Aggregated result of the audit phase."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


def run_audit(
    source_dir: Path,
    ngc: NudgisClient,
    ids_to_process: list[str] | None = None,
    ffprobe: str = 'ffprobe',
) -> tuple[Report, dict[str, MediaGroup]]:
    """Run the whole audit phase and return the report and the media groups."""
    report = Report()
    groups = scan_source_tree(source_dir, ids_to_process, report)
    validate_metadata_csv(source_dir / METADATA_FILENAME, groups, ids_to_process, report)
    validate_annotations_csv(source_dir, groups, ids_to_process, report)
    validate_media_integrity(groups, report, ffprobe=ffprobe)
    server_checks(ngc, groups, report)
    return report, groups


def scan_source_tree(
    source_dir: Path,
    ids_to_process: list[str] | None,
    report: Report,
) -> dict[str, MediaGroup]:
    """
    Walk ``source_dir`` and group the files by media (source id).

    The ``annotations`` directory and the ``metadata.csv`` file are ignored here; they are
    validated separately. Structural problems are added to ``report``.
    """
    logger.info('Scanning source tree "%s".', source_dir)
    annotations_dir = source_dir / ANNOTATIONS_DIRNAME

    media_files: list[Path] = []
    other_files: list[Path] = []
    for path in sorted(source_dir.rglob('*')):
        if not path.is_file():
            continue
        if path == source_dir / METADATA_FILENAME:
            continue
        if annotations_dir in path.parents or path == annotations_dir:
            continue
        if len(path.name) > MAX_FIELD_LENGTH:
            report.error(f'File name too long: "{path.name}".')
            continue
        if ids_to_process is not None and not any(path.stem.startswith(source_id) for source_id in ids_to_process):
            continue
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            media_files.append(path)
        else:
            other_files.append(path)

    # First pass: determine the main media files and detect multi-stream secondary files.
    video_stems = {path.stem for path in media_files}
    groups: dict[str, MediaGroup] = {}
    secondary_files: list[Path] = []
    for path in media_files:
        match = STREAM_SUFFIX_RE.match(path.stem)
        if match and match.group(1) in video_stems:
            secondary_files.append(path)
            continue
        source_id = path.stem
        if source_id in groups:
            report.error(
                f'Duplicate source id "{source_id}": "{path}" and '
                f'"{groups[source_id].main_file}".'
            )
            continue
        if ids_to_process is not None and source_id not in ids_to_process:
            continue
        groups[source_id] = MediaGroup(
            source_id=source_id,
            object_id=None,
            main_file=path,
            rel_dir=path.parent.relative_to(source_dir),
        )

    # Second pass: attach the multi-stream secondary files to their main media.
    for path in secondary_files:
        source_id, index_str = STREAM_SUFFIX_RE.match(path.stem).groups()
        if ids_to_process is not None and source_id not in ids_to_process:
            continue
        index = int(index_str)
        group = groups.get(source_id)
        if group is None:
            report.error(f'Multi-stream file "{path}" has no main media "{source_id}".')
            continue
        if index < 2 or index > MAX_STREAMS:
            report.error(
                f'Multi-stream file "{path}" uses an out-of-range index '
                f'(must be between 2 and {MAX_STREAMS}).'
            )
            continue
        group.extra_streams[index] = path

    # Third pass: attach the linked subtitle and audio files to their main media.
    for path in other_files:
        suffix = path.suffix.lower()
        if suffix not in SUBTITLE_EXTENSIONS and suffix not in AUDIO_EXTENSIONS:
            report.warning(f'Ignoring unexpected file "{path}".')
            continue
        match = LANG_SUFFIX_RE.match(path.stem)
        if not match:
            report.warning(
                f'Ignoring file "{path}" because it does not follow the "<id>_<lang>" naming rule.'
            )
            continue
        source_id, lang = match.groups()
        if ids_to_process is not None and source_id not in ids_to_process:
            continue
        group = groups.get(source_id)
        if group is None:
            report.error(f'The file "{path}" has no main media "{source_id}".')
            continue
        target = group.subtitles if suffix in SUBTITLE_EXTENSIONS else group.audio_tracks
        if lang in target:
            file_type = 'subtitle' if suffix in SUBTITLE_EXTENSIONS else 'audio track'
            report.error(f'Duplicate {file_type} file for "{source_id}" and language "{lang}".')
            continue
        target[lang] = path

    # Report multi-stream media (handled by the dedicated compose_multistream.py script).
    for group in groups.values():
        if group.extra_streams:
            indexes = sorted(group.extra_streams)
            if indexes != list(range(2, 2 + len(indexes))):
                report.warning(
                    f'Multi-stream media "{group.source_id}" has non-contiguous stream '
                    f'indexes: {indexes}.'
                )
            report.warning(
                f'Multi-stream media "{group.source_id}" ({1 + len(indexes)} streams) will '
                'be combined with ffmpeg during import.'
            )

    if not groups:
        report.error(f'No media file to import found in "{source_dir}".')
    return groups


def _split_pipe(value: str, drop_empty: bool = True) -> list[str]:
    """Split a pipe-separated metadata value, optionally dropping empty items."""
    return [item.strip() for item in value.split('|') if not drop_empty or item.strip()]


def validate_metadata_csv(
    csv_path: Path,
    groups: dict[str, MediaGroup],
    ids_to_process: list[str] | None,
    report: Report,
) -> None:
    """
    Validate ``metadata.csv`` and attach each valid row to its media group.

    The file is optional; if it is missing, nothing is attached.
    """
    logger.info('Validating metadata CSV "%s".', csv_path)
    if not csv_path.is_file():
        report.warning(f'No "{METADATA_FILENAME}" file found at "{csv_path}".')
        return

    with csv_path.open('r', encoding='utf-8', newline='') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=',', quotechar='"')

        header = reader.fieldnames or []
        missing = METADATA_MANDATORY_FIELDS - set(header)
        if missing:
            report.error(
                f'"{METADATA_FILENAME}" is missing mandatory columns: '
                f'{", ".join(sorted(missing))}.'
            )
            return

        for unknown in sorted(set(header) - METADATA_KNOWN_FIELDS):
            report.warning(f'"{METADATA_FILENAME}" has an unknown column "{unknown}".')

        slugs: dict[str, str] = {}
        seen_ids: set[str] = set()
        for line, row in enumerate(reader, start=2):
            source_id = (row.get('source_id') or '').strip()
            if not source_id:
                report.error(f'{METADATA_FILENAME}:{line}: empty "source_id".')
                continue
            if ids_to_process is not None and source_id not in ids_to_process:
                continue
            if source_id in seen_ids:
                report.error(
                    f'{METADATA_FILENAME}:{line}: duplicate "source_id" "{source_id}".'
                )
                continue
            seen_ids.add(source_id)

            group = groups.get(source_id)
            if group is None:
                report.error(
                    f'{METADATA_FILENAME}:{line}: "source_id" "{source_id}" has no '
                    'matching media file.'
                )

            if not (row.get('title') or '').strip():
                report.error(f'{METADATA_FILENAME}:{line}: empty "title".')

            slug = (row.get('slug') or '').strip().lower()
            if slug:
                if not SLUG_RE.match(slug):
                    report.error(
                        f'{METADATA_FILENAME}:{line}: invalid "slug" "{slug}" (allowed '
                        'characters: a-z, 0-9, "-", "_").'
                    )
                elif slug in slugs:
                    report.error(
                        f'{METADATA_FILENAME}:{line}: "slug" "{slug}" is already used by '
                        f'"{slugs[slug]}".'
                    )
                else:
                    row['slug'] = slug
                    slugs[slug] = source_id

            language = (row.get('language') or '').strip()
            if language and not LANGUAGE_RE.match(language):
                report.error(
                    f'{METADATA_FILENAME}:{line}: invalid "language" "{language}" '
                    '(expected a 3-letter ISO 639-2 code).'
                )

            creation = (row.get('creation') or '').strip()
            if creation:
                try:
                    datetime.strptime(creation, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    report.error(
                        f'{METADATA_FILENAME}:{line}: invalid "creation" "{creation}" '
                        '(expected format YYYY-MM-DDTHH:MM:SS).'
                    )

            for field_name in (
                'title', 'slug', 'keywords', 'company_name', 'company_url', 'license_name', 'license_url',
            ):
                value = (row.get(field_name) or '').strip()
                if len(value) > MAX_FIELD_LENGTH:
                    report.error(
                        f'{METADATA_FILENAME}:{line}: "{field_name}" value too long: "{value}".'
                    )

            names = _split_pipe((row.get('speaker_name') or ''), drop_empty=False)
            emails = _split_pipe((row.get('speaker_email') or ''), drop_empty=False)
            if len(names) != len(emails):
                report.error(
                    f'{METADATA_FILENAME}:{line}: "speaker_name" ({len(names)}) and '
                    f'"speaker_email" ({len(emails)}) have a different number of values.'
                )

            for field_name in ('validated', 'unlisted', 'detect_slides'):
                value = (row.get(field_name) or '').strip()
                if value and value not in YESNO_VALUES:
                    report.error(
                        f'{METADATA_FILENAME}:{line}: invalid "{field_name}" "{value}" '
                        '(expected "yes" or "no").'
                    )

            if group is not None:
                group.metadata = row

    for source_id, group in groups.items():
        if group.metadata is None:
            report.warning(
                f'Media "{source_id}" has no row in "{METADATA_FILENAME}".'
            )


def validate_annotations_csv(
    source_dir: Path,
    groups: dict[str, MediaGroup],
    ids_to_process: list[str] | None,
    report: Report,
) -> None:
    """
    Validate ``annotations/annotations.csv`` and attach each row to its media group.

    The annotations directory is optional; if it is missing, nothing is attached.
    """
    annotations_dir = source_dir / ANNOTATIONS_DIRNAME
    csv_path = annotations_dir / ANNOTATIONS_FILENAME
    logger.info('Validating annotations CSV "%s".', csv_path)
    if not annotations_dir.is_dir():
        return
    if not csv_path.is_file():
        report.error(f'No "{ANNOTATIONS_FILENAME}" file found in "{annotations_dir}".')
        return

    with csv_path.open('r', encoding='utf-8', newline='') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=',', quotechar='"')

        header = reader.fieldnames or []
        missing = ANNOTATIONS_MANDATORY_FIELDS - set(header)
        if missing:
            report.error(
                f'"{ANNOTATIONS_FILENAME}" is missing mandatory columns: '
                f'{", ".join(sorted(missing))}.'
            )
            return

        for unknown in sorted(set(header) - ANNOTATIONS_KNOWN_FIELDS):
            report.warning(f'"{ANNOTATIONS_FILENAME}" has an unknown column "{unknown}".')

        for line, row in enumerate(reader, start=2):
            source_id = (row.get('source_id') or '').strip()
            if not source_id:
                report.error(f'{ANNOTATIONS_FILENAME}:{line}: empty "source_id".')
                continue
            if ids_to_process is not None and source_id not in ids_to_process:
                continue

            group = groups.get(source_id)
            if group is None:
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: "source_id" "{source_id}" has no '
                    'matching media file.'
                )

            for field_name in (
                'type', 'time', 'title', 'keywords', 'attachment',
            ):
                value = (row.get(field_name) or '').strip()
                if field_name in ('title', 'keywords'):
                    # The value is escaped in Nudgis, which increases its length
                    value = escape(value)
                if len(value) > MAX_FIELD_LENGTH:
                    report.error(
                        f'{ANNOTATIONS_FILENAME}:{line}: "{field_name}" value too long: "{value}".'
                    )

            ann_type = (row.get('type') or '').strip().lower()
            row['type'] = ann_type
            if not ann_type:
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: "type" cannot be empty.'
                )
            elif ann_type == 'chapter' and not (row.get('title') or '').strip():
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: a "chapter" annotation requires a '
                    '"title".'
                )

            time_value = (row.get('time') or '').strip() or '0'
            row['time'] = time_value
            if time_value and not time_value.isdigit():
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: invalid "time" "{time_value}" '
                    '(expected an integer number of milliseconds).'
                )

            attachment = (row.get('attachment') or '').strip().strip('/')
            if attachment:
                attachment = attachment.removeprefix(ANNOTATIONS_DIRNAME + '/')
                row['attachment'] = attachment
            if ann_type == 'chapter' and attachment:
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment cannot be linked to "chapter" annotations.'
                )
            elif ann_type == 'slide' and not attachment:
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment is required for "slide" annotations.'
                )
            elif attachment and not (annotations_dir / attachment).is_file():
                report.error(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment "{attachment}" does not '
                    'exist in the annotations directory.'
                )

            if group is not None:
                group.annotations.append(row)


def validate_media_integrity(
    groups: dict[str, MediaGroup],
    report: Report,
    ffprobe: str = 'ffprobe',
) -> None:
    """Run ``ffprobe`` on every video and audio file of every media group."""
    for group in groups.values():
        files = [group.main_file, *group.extra_streams.values(), *group.audio_tracks.values()]
        for path in files:
            error = probe_media(path, ffprobe=ffprobe)
            if error:
                report.error(f'Invalid media file "{path}": {error}')


def probe_media(path: Path, ffprobe: str = 'ffprobe') -> str | None:
    """
    Check the integrity of a media file using ``ffprobe``.

    Return ``None`` if the file is a valid media, or an error message otherwise.
    """
    try:
        result = subprocess.run(
            [
                ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type',
                '-of', 'json', str(path),
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return f'"{ffprobe}" command not found; cannot check media integrity.'
    if result.returncode != 0:
        return f'ffprobe failed: {result.stderr.strip()}'
    try:
        streams = json.loads(result.stdout).get('streams', [])
    except json.JSONDecodeError:
        return 'ffprobe returned an invalid output.'
    if not streams:
        return 'no media stream found.'
    return None


def server_checks(
    ngc: NudgisClient,
    groups: dict[str, MediaGroup],
    report: Report,
) -> None:
    """Run the server-side audit checks (slugs, annotation types, speakers, channels)."""
    try:
        ngc.check_server()
    except Exception as err:
        report.error(f'Cannot reach the Nudgis server: {err}')
        return

    # Slug and external ref uniqueness against the existing catalog.
    if groups:
        try:
            catalog = ngc.get_catalog(fmt='flat')
        except NudgisRequestError as err:
            report.warning(f'Could not fetch the catalog to check slugs: {err}')
        else:
            existing_slugs = {
                obj['slug']: obj['oid']
                for key in ('channels', 'videos', 'lives', 'photos')
                for obj in catalog.get(key, [])
                if obj.get('slug')
            }
            existing_ext_refs = {
                obj['external_ref']: obj['oid']
                for obj in catalog.get('videos', [])
                if obj.get('external_ref')
            }
            for group in groups.values():
                if group.external_ref and group.external_ref in existing_ext_refs:
                    group.object_id = existing_ext_refs[group.external_ref]
                    logger.info(
                        'Found existing video for external_ref "%s": %s',
                        group.external_ref, group.object_id
                    )
                else:
                    slug = (group.metadata.get('slug') or '').strip() if group.metadata else ''
                    if slug and slug in existing_slugs:
                        report.warning(
                            f'Slug "{slug}" (media "{group.source_id}") already exists on the server'
                            f' (used by "{existing_slugs[slug]}").'
                        )

    # Annotation types existence.
    used_types = {
        ann_type
        for group in groups.values()
        for row in group.annotations
        if (ann_type := (row.get('type') or '').strip())
        and ann_type not in INTERNAL_ANNOTATION_TYPES
    }
    if used_types:
        try:
            response = ngc.api('annotations/types/list/')
        except NudgisRequestError as err:
            report.warning(f'Could not fetch annotation types: {err}')
        else:
            server_types = {
                value['slug']
                for value in response.get('types', [])
                if value.get('slug')
            }
            for ann_type in sorted(used_types - server_types):
                report.error(
                    f'Annotation type "{ann_type}" does not exist on the server.'
                )

    # Speaker resolution for "mscspeaker" destinations.
    speaker_emails: set[str] = set()
    for group in groups.values():
        row = group.metadata
        if row and (row.get('channel') or '').strip() == 'mscspeaker':
            emails = _split_pipe(row.get('speaker_email') or '')
            if not emails:
                report.error(
                    f'Media "{group.source_id}" targets "mscspeaker" but has no '
                    '"speaker_email".'
                )
            speaker_emails.update(emails)
    for email in sorted(speaker_emails):
        try:
            response = ngc.api('users/', params={'search': email, 'limit': 1})
        except NudgisRequestError as err:
            report.warning(f'Could not check speaker "{email}": {err}')
        else:
            if not response.get('users'):
                report.error(f'Speaker email "{email}" does not match any Nudgis user.')

    # Existence of explicitly referenced channels (by oid or slug).
    for group in groups.values():
        row = group.metadata
        channel = (row.get('channel') or '').strip() if row else ''
        if not channel:
            continue
        params = None
        if channel == 'mscspeaker':
            continue
        elif channel.startswith('mscid-'):
            params = {'oid': channel[len('mscid-'):]}
        elif not channel.startswith('mscpath-'):
            params = {'slug': channel}
        if params is None:
            continue
        try:
            ngc.api('channels/get/', params=params)
        except NudgisRequestError as err:
            report.error(
                f'Failed to get channel "{channel}" for media "{group.source_id}": {err}'
            )

    # Existence of audio tracks (to skip already imported audio tracks).
    for group in groups.values():
        if not group.object_id:
            continue
        try:
            response = ngc.api('medias/audio/tracks/list/', params={'oid': group.object_id})
        except NudgisRequestError as err:
            if err.status_code == 404:
                continue
            report.error(
                f'Failed to get audio tracks for media "{group.source_id}": {err}'
            )
        else:
            for track in response.get('audio_tracks', []):
                if track.get('is_original'):
                    continue
                group.existing_elements.append(f'audio:{track["language"]}')

    # Existence of subtitles (to skip already imported subtitles).
    for group in groups.values():
        if not group.object_id:
            continue
        try:
            response = ngc.api('subtitles/', params={'oid': group.object_id})
        except NudgisRequestError as err:
            if err.status_code == 404:
                continue
            report.error(
                f'Failed to get subtitles for media "{group.source_id}": {err}'
            )
        else:
            for subtitle in response.get('subtitles', []):
                if subtitle.get('auto_transcripted') or subtitle.get('auto_translated'):
                    continue
                group.existing_elements.append(f'subtitle:{subtitle["lang_code"]}')

    # Existence of annotations (to skip already imported annotations).
    for group in groups.values():
        if not group.object_id:
            continue
        try:
            response = ngc.api('annotations/list/', params={'oid': group.object_id})
        except NudgisRequestError as err:
            if err.status_code == 404:
                continue
            report.error(
                f'Failed to get annotations for media "{group.source_id}": {err}'
            )
        else:
            type_by_id = {t['id']: t['slug'] for t in response.get('types', {}).values()}
            for annotation in response.get('annotations', []):
                type_slug = type_by_id[annotation['type_id']]
                group.existing_elements.append(f'annotation:{type_slug}:{annotation["time"]}')


def import_media(
    ngc: NudgisClient,
    groups: dict[str, MediaGroup],
    main_channel: str,
    source_dir: Path,
    mapping_file: Path,
    temp_dir: Path,
    ffmpeg: str = 'ffmpeg',
    ffprobe: str = 'ffprobe',
) -> dict[str, str]:
    """Import every media group and return the ``source_id`` -> ``oid`` mapping."""
    failures: dict[str, str] = {}
    mapping: dict[str, str] = {}
    for source_id, group in groups.items():
        try:
            channel = resolve_channel(main_channel, group.rel_dir, group.metadata)
            metadata = build_media_metadata(group.metadata)
            metadata['external_ref'] = group.external_ref
            if group.object_id:
                oid = group.object_id
                logger.info('Media "%s" already exists with oid "%s".', source_id, oid)
            else:
                file_path = group.main_file
                if group.extra_streams:
                    # Combine the multi-stream files into a single side-by-side video.
                    streams = [group.main_file] + [
                        group.extra_streams[index] for index in sorted(group.extra_streams)
                    ]
                    file_path = temp_dir / group.main_file.name
                    logger.info(
                        'Composing %s streams of multi-stream media "%s".',
                        len(streams), source_id,
                    )
                    compose_streams(streams, file_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
                    # The composition layout preset is written next to the composed video.
                    metadata['layout_preset'] = file_path.with_suffix('.json').read_text()
                logger.info('Uploading media "%s" into channel "%s".', source_id, channel)
                response = ngc.add_media(
                    title=metadata.pop('title', source_id),
                    file_path=file_path,
                    channel=channel,
                    origin=metadata['external_ref'],
                    skip_automatic_subtitles='yes',
                    skip_automatic_enrichments='yes',
                    **metadata,
                )
                oid = response['oid']
                group.object_id = oid
                logger.info('Media "%s" created with oid "%s".', source_id, oid)
                if file_path != group.main_file:
                    # The composed video and its layout preset have been uploaded; clean up.
                    file_path.unlink(missing_ok=True)
                    file_path.with_suffix('.json').unlink(missing_ok=True)
                    logger.debug('Removed temporary composed files for "%s".', source_id)

            for lang, path in group.audio_tracks.items():
                if f'audio:{lang}' in group.existing_elements:
                    logger.info('The "%s" audio track already exists in "%s".', lang, oid)
                    continue
                logger.info('Adding "%s" audio track to "%s".', lang, oid)
                with path.open('rb') as fileobj:
                    # Known limitation: the audio track must have a duration close to the video duration
                    ngc.api(
                        'medias/audio/tracks/add/',
                        method='post',
                        data={'oid': oid, 'lang': lang},
                        files={'file': (path.name, fileobj)},
                    )

            for lang, path in group.subtitles.items():
                if f'subtitle:{lang}' in group.existing_elements:
                    logger.info('The "%s" subtitles already exists in "%s".', lang, oid)
                    continue
                logger.info('Adding "%s" subtitles to "%s".', lang, oid)
                with path.open('rb') as fileobj:
                    ngc.api(
                        'subtitles/add/',
                        method='post',
                        data={'oid': oid, 'lang': lang},
                        files={'file': (path.name, fileobj)},
                    )

            for row in group.annotations:
                if 'time' in row and 'type' in row:
                    if f'annotation:{row["type"]}:{row["time"]}' in group.existing_elements:
                        logger.info(
                            'The "%s" annotation at time "%s" already exists in "%s".',
                            row['type'], row['time'], oid,
                        )
                        continue
                    post_annotation(ngc, oid, row, source_dir)

            # Only record the media as imported once all its elements succeeded.
            mapping[source_id] = oid
        except (NudgisRequestError, RuntimeError) as err:
            logger.error('Failed to import media "%s": %s', source_id, err)
            failures[source_id] = str(err)

    mapping_file.write_text(
        'source_id,oid\n' + ''.join(f'{src},{oid}\n' for src, oid in mapping.items()),
    )
    logger.info('Wrote mapping of %s media to "%s".', len(mapping), mapping_file)

    if failures:
        logger.warning(
            'List of failed imports:\n  - %s',
            '\n  - '.join(f'{src}: {msg}' for src, msg in failures.items()),
        )
    logger.info('Media import complete: %s succeeded, %s failed.', len(mapping), len(failures))
    return mapping


def resolve_channel(
    main_channel: str,
    rel_dir: Path,
    row: dict[str, str] | None,
) -> str:
    """
    Determine the destination channel of a media.

    The channel column of ``metadata.csv`` takes precedence; otherwise the folder tree is
    mirrored below the main migration channel using an "mscpath" identifier.
    """
    if row:
        channel = (row.get('channel') or '').strip()
        if channel:
            return channel
    base = main_channel[len('mscpath-'):] if main_channel.startswith('mscpath-') else main_channel
    parts = [base, *rel_dir.parts]
    return 'mscpath-' + '/'.join(parts)


def build_media_metadata(row: dict[str, str] | None) -> dict[str, str]:
    """
    Build the ``medias/add/`` metadata payload from a ``metadata.csv`` row.

    The source id is always copied to the "external reference" field.
    Pipe-separated lists are converted to the format expected by the API.
    """
    metadata: dict[str, str] = {}
    if not row:
        return metadata
    # Simple pass-through string fields (spec column -> API parameter name).
    passthrough = {
        'title': 'title',
        'slug': 'slug',
        'description': 'description',
        'language': 'language',
        'creation': 'creation',
        'company_name': 'company',
        'company_url': 'company_url',
        'license_name': 'license',
        'license_url': 'license_url',
        'validated': 'validated',
        'unlisted': 'unlisted',
        'detect_slides': 'detect_slides',
    }
    for column, param in passthrough.items():
        value = (row.get(column) or '').strip()
        if value:
            metadata[param] = value
    # Keywords are stored as a space-separated list on Nudgis.
    keywords = _split_pipe(row.get('keywords') or '')
    if keywords:
        metadata['keywords'] = ','.join(keywords)
    # Categories are stored as a newline-separated list on Nudgis.
    categories = _split_pipe(row.get('categories') or '')
    if categories:
        metadata['category'] = '\n'.join(categories)
    # Speakers use a pipe-separated list (same convention as the rest of the API).
    names = _split_pipe((row.get('speaker_name') or ''), drop_empty=False)
    emails = _split_pipe((row.get('speaker_email') or ''), drop_empty=False)
    if names:
        metadata['speaker'] = '|'.join(names)
    if emails:
        metadata['speaker_email'] = '|'.join(emails)
    return metadata


def post_annotation(
    ngc: NudgisClient,
    oid: str,
    row: dict[str, str],
    source_dir: Path,
) -> None:
    """Post a single annotation (optionally with an attachment) to a media."""
    data: dict[str, str] = {'oid': oid}
    ann_type = row['type']
    data['type_slug'] = ann_type
    for column in ('time', 'title', 'content'):
        value = (row.get(column) or '').strip()
        if value:
            data[column] = value
    keywords = _split_pipe(row.get('keywords') or '')
    if keywords:
        data['keywords'] = ','.join(keywords)
    attachment = (row.get('attachment') or '').strip()
    if ann_type not in INTERNAL_ANNOTATION_TYPES and not data.get('content'):
        data['content'] = '-'
    logger.info('Adding "%s" annotation at "%s" to "%s".', ann_type, data.get('time'), oid)
    if attachment:
        path = source_dir / ANNOTATIONS_DIRNAME / attachment
        with path.open('rb') as fileobj:
            ngc.api(
                'annotations/post/',
                method='post',
                data=data,
                files={'attachment': (path.name, fileobj)},
            )
    else:
        ngc.api('annotations/post/', method='post', data=data)


def mass_import(sys_args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        'mass_import',
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        help='Path to the configuration file (e.g. myconfig.json).',
        required=True,
    )
    parser.add_argument(
        '--source-dir',
        help='Path to the directory containing the media to import.',
        required=True,
        type=Path,
    )
    parser.add_argument(
        '--ids-to-process',
        help='Comma-separated list of source file IDs to import. '
             'By default, all source files are imported.',
        default=None,
        type=str,
    )
    parser.add_argument(
        '--channel',
        help='Main migration channel (a title or an "mscpath-..."/"mscid-..." identifier).',
        required=True,
    )
    parser.add_argument(
        '--apply',
        help='Actually perform the import. Without this flag, the script only audits the '
             'source directory (file tree, CSV files, media integrity and server checks).',
        action='store_true',
    )
    parser.add_argument(
        '--mapping-file',
        help='Path of the produced "source_id,oid" mapping CSV file.',
        default=Path(f'./mapping_{date.today().strftime("%Y-%m-%d")}.csv'),
        type=Path,
    )
    parser.add_argument(
        '--ffprobe',
        help='Path to the ffprobe executable used to check media integrity.',
        default='ffprobe',
    )
    parser.add_argument(
        '--ffmpeg',
        help='Path to the ffmpeg executable used to combine multi-stream media.',
        default='ffmpeg',
    )
    parser.add_argument(
        '--temp-dir',
        help='Directory for the temporary videos produced when combining multi-stream media.',
        default=Path('./temp'),
        type=Path,
    )
    parser.add_argument(
        '--log-level',
        help='Log level.',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug'],
    )
    args = parser.parse_args(sys_args)

    configure_logging(args.log_level.upper())

    source_dir = args.source_dir
    if not source_dir.is_dir():
        logger.error('Source directory "%s" does not exist.', source_dir)
        return 1
    ids_to_process = args.ids_to_process.split(',') if args.ids_to_process else None

    ngc = NudgisClient(args.conf, setup_logging=False)
    ngc.conf['TIMEOUT'] = max(600, ngc.conf['TIMEOUT'])

    report, groups = run_audit(source_dir, ngc, ids_to_process, ffprobe=args.ffprobe)

    logger.info('Audit completed.')
    for warning in report.warnings:
        logger.warning(warning)
    for error in report.errors:
        logger.error(error)

    if not report.ok:
        logger.error(
            'Audit failed with %s error(s) and %s warning(s); aborting.',
            len(report.errors), len(report.warnings),
        )
        return 1

    if not args.apply:
        logger.info(
            'Audit succeeded for %s media (dry-run). Re-run with "--apply" to import.',
            len(groups),
        )
        return 0

    import_media(
        ngc,
        groups,
        args.channel,
        source_dir,
        args.mapping_file,
        args.temp_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
    )
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(mass_import(sys.argv[1:]))
