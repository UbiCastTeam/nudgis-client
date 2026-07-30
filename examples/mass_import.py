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

Problems found during the audit never abort the whole run: recoverable issues are cleaned
up in place (invalid optional fields are dropped, over-long values are truncated) and the
individual media, metadata entries or annotations that cannot be processed are skipped so
that the rest is still imported. Both modes end with a report summarising the content that
was correctly processed and the content that could not be. Only unrecoverable problems (an
unreachable server, a structurally broken CSV, a missing ``ffprobe`` or an empty source
tree) still abort the run.

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
MAX_FIELD_LENGTH = 200
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
    """
    Fatal errors and warnings collected during the audit phase.

    Only *unrecoverable* problems (an unreachable server, a structurally broken CSV, an
    empty source tree) are recorded as ``errors`` and abort the whole run. Everything that
    can be cleaned up or skipped without blocking the other media is either fixed in place
    (with a ``warning``) or accounted for in the :class:`Summary`.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


# Reasons why a media cannot be imported at all (used as keys of ``Summary.unimportable_media``).
UNIMPORTABLE_CORRUPTED = 'corrupted files (ffprobe)'
UNIMPORTABLE_FILENAME = 'invalid file name'
UNIMPORTABLE_DUPLICATE = 'duplicates'
UNIMPORTABLE_NO_SPEAKER = 'personal channel target without recipient'


@dataclass
class Summary:
    """Counters aggregated across the audit and import phases for the final report."""

    # Correctly processed content.
    merged_videos: int = 0
    medias_imported: int = 0
    medias_existing: int = 0
    metadata_applied: int = 0
    # Linked elements, broken down into already existing / imported / failed.
    audio_tracks_existing: int = 0
    audio_tracks_imported: int = 0
    audio_tracks_failed: int = 0
    subtitles_existing: int = 0
    subtitles_imported: int = 0
    subtitles_failed: int = 0
    annotations_existing: int = 0
    annotations_imported: int = 0
    annotations_failed: int = 0
    # Unprocessable content.
    unimportable_media: dict[str, list[str]] = field(default_factory=dict)
    invalid_metadata: int = 0
    unimportable_annotations: int = 0
    import_failures: int = 0

    def drop_media(self, source_id: str, reason: str) -> None:
        """Record a media that cannot be imported under the given ``reason``."""
        self.unimportable_media.setdefault(reason, []).append(source_id)


def run_audit(
    source_dir: Path,
    ngc: NudgisClient,
    ids_to_process: list[str] | None = None,
    ffprobe: str = 'ffprobe',
    apply: bool = False,
) -> tuple[Report, Summary, dict[str, MediaGroup]]:
    """
    Run the whole audit phase and return the report, the summary and the media groups.

    Instead of aborting on the first problem, the audit cleans up recoverable issues in
    place and drops the individual media, metadata entries or annotations that cannot be
    processed (accounting for them in ``summary``) so that the other content can still be
    imported. Only unrecoverable problems end up in ``report.errors``.

    When ``apply`` is true the server-side checks are allowed to fix the catalog (create
    missing speaker users and grant them the personal-channel permission); otherwise they
    only report what would be done.
    """
    report = Report()
    summary = Summary()
    groups = scan_source_tree(source_dir, ids_to_process, report, summary)
    validate_metadata_csv(source_dir / METADATA_FILENAME, groups, ids_to_process, report, summary)
    validate_annotations_csv(source_dir, groups, ids_to_process, report, summary)
    validate_media_integrity(groups, report, summary, ffprobe=ffprobe)
    server_checks(ngc, groups, report, summary, apply=apply)
    return report, summary, groups


def scan_source_tree(
    source_dir: Path,
    ids_to_process: list[str] | None,
    report: Report,
    summary: Summary,
) -> dict[str, MediaGroup]:
    """
    Walk ``source_dir`` and group the files by media (source id).

    The ``annotations`` directory and the ``metadata.csv`` file are ignored here; they are
    validated separately. Media that cannot be imported (a video with a too-long name or a
    duplicate source id) are dropped and counted in ``summary``; auxiliary files that cannot
    be attached are simply ignored with a warning.
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
        if ids_to_process is not None and not any(path.stem.startswith(source_id) for source_id in ids_to_process):
            continue
        suffix = path.suffix.lower()
        if len(path.name) > MAX_FIELD_LENGTH:
            # A video with a too-long name is a media we cannot import; anything else is
            # an auxiliary file we can simply ignore.
            if suffix in VIDEO_EXTENSIONS:
                report.warning(f'Media file name too long, media skipped: "{path.name}".')
                summary.drop_media(path.stem, UNIMPORTABLE_FILENAME)
            else:
                report.warning(f'Ignoring file with a too-long name: "{path.name}".')
            continue
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
            report.warning(
                f'Duplicate source id "{source_id}", media skipped: "{path}" (kept '
                f'"{groups[source_id].main_file}").'
            )
            summary.drop_media(source_id, UNIMPORTABLE_DUPLICATE)
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
            report.warning(f'Ignoring multi-stream file "{path}" with no main media "{source_id}".')
            continue
        if index < 2 or index > MAX_STREAMS:
            report.warning(
                f'Ignoring multi-stream file "{path}" with an out-of-range index '
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
            report.warning(f'Ignoring file "{path}" with no main media "{source_id}".')
            continue
        target = group.subtitles if suffix in SUBTITLE_EXTENSIONS else group.audio_tracks
        if lang in target:
            file_type = 'subtitle' if suffix in SUBTITLE_EXTENSIONS else 'audio track'
            report.warning(
                f'Ignoring duplicate {file_type} file for "{source_id}" and language "{lang}": "{path}".'
            )
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
    summary: Summary,
) -> None:
    """
    Validate ``metadata.csv``, clean up its rows and attach each usable row to its media.

    The file is optional; if it is missing, nothing is attached. A row that cannot be tied
    to a media (empty, duplicate or unknown ``source_id``) is dropped and counted as an
    invalid metadata entry. Individual invalid fields are cleaned up in place (dropped or
    truncated) so that the media can still be imported with the rest of its metadata.
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
                report.warning(f'{METADATA_FILENAME}:{line}: empty "source_id", row ignored.')
                summary.invalid_metadata += 1
                continue
            if ids_to_process is not None and source_id not in ids_to_process:
                continue
            if source_id in seen_ids:
                report.warning(
                    f'{METADATA_FILENAME}:{line}: duplicate "source_id" "{source_id}", row ignored.'
                )
                summary.invalid_metadata += 1
                continue
            seen_ids.add(source_id)

            group = groups.get(source_id)
            if group is None:
                report.warning(
                    f'{METADATA_FILENAME}:{line}: "source_id" "{source_id}" has no matching '
                    'media file, row ignored.'
                )
                summary.invalid_metadata += 1
                continue

            if not (row.get('title') or '').strip():
                # The title is mandatory; fall back to the source id at import time.
                report.warning(
                    f'{METADATA_FILENAME}:{line}: empty "title", the source id will be used.'
                )
                row['title'] = ''

            slug = (row.get('slug') or '').strip().lower()
            if slug and not SLUG_RE.match(slug):
                report.warning(
                    f'{METADATA_FILENAME}:{line}: invalid "slug" "{slug}" ignored (allowed '
                    'characters: a-z, 0-9, "-", "_").'
                )
                slug = ''
            elif slug and slug in slugs:
                report.warning(
                    f'{METADATA_FILENAME}:{line}: "slug" "{slug}" already used by '
                    f'"{slugs[slug]}", ignored.'
                )
                slug = ''
            elif slug:
                slugs[slug] = source_id
            row['slug'] = slug

            language = (row.get('language') or '').strip()
            if language and not LANGUAGE_RE.match(language):
                report.warning(
                    f'{METADATA_FILENAME}:{line}: invalid "language" "{language}" ignored '
                    '(expected a 3-letter ISO 639-2 code).'
                )
                row['language'] = ''

            creation = (row.get('creation') or '').strip()
            if creation:
                try:
                    datetime.strptime(creation, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    report.warning(
                        f'{METADATA_FILENAME}:{line}: invalid "creation" "{creation}" ignored '
                        '(expected format YYYY-MM-DDTHH:MM:SS).'
                    )
                    row['creation'] = ''

            for field_name in (
                'title', 'slug', 'keywords', 'company_name', 'company_url', 'license_name', 'license_url',
            ):
                value = (row.get(field_name) or '').strip()
                if len(value) > MAX_FIELD_LENGTH:
                    report.warning(
                        f'{METADATA_FILENAME}:{line}: "{field_name}" value too long, truncated.'
                    )
                    row[field_name] = value[:MAX_FIELD_LENGTH]

            names = _split_pipe((row.get('speaker_name') or ''), drop_empty=False)
            emails = _split_pipe((row.get('speaker_email') or ''), drop_empty=False)
            if len(names) != len(emails):
                report.warning(
                    f'{METADATA_FILENAME}:{line}: "speaker_name" ({len(names)}) and '
                    f'"speaker_email" ({len(emails)}) have a different number of values; '
                    'speakers ignored.'
                )
                row['speaker_name'] = ''
                row['speaker_email'] = ''

            for field_name in ('validated', 'unlisted', 'detect_slides'):
                value = (row.get(field_name) or '').strip()
                if value and value not in YESNO_VALUES:
                    report.warning(
                        f'{METADATA_FILENAME}:{line}: invalid "{field_name}" "{value}" ignored '
                        '(expected "yes" or "no").'
                    )
                    row[field_name] = ''

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
    summary: Summary,
) -> None:
    """
    Validate ``annotations/annotations.csv``, clean up its rows and attach the usable ones.

    The annotations directory is optional; if it is missing, nothing is attached. An
    annotation that cannot be imported (no matching media, empty type, ``chapter`` without a
    title, ``slide`` without an attachment, missing attachment file) is dropped and counted;
    recoverable issues (too-long fields, invalid time, an attachment on a ``chapter``) are
    cleaned up in place.
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
                report.warning(f'{ANNOTATIONS_FILENAME}:{line}: empty "source_id", annotation ignored.')
                summary.unimportable_annotations += 1
                continue
            if ids_to_process is not None and source_id not in ids_to_process:
                continue

            group = groups.get(source_id)
            if group is None:
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: "source_id" "{source_id}" has no matching '
                    'media file, annotation ignored.'
                )
                summary.unimportable_annotations += 1
                continue

            for field_name in ('type', 'time', 'title', 'keywords', 'attachment'):
                value = (row.get(field_name) or '').strip()
                # The title and keywords are escaped in Nudgis, which increases their length.
                escaped = field_name in ('title', 'keywords')
                if len(escape(value) if escaped else value) > MAX_FIELD_LENGTH:
                    report.warning(
                        f'{ANNOTATIONS_FILENAME}:{line}: "{field_name}" value too long, truncated.'
                    )
                    while len(escape(value) if escaped else value) > MAX_FIELD_LENGTH:
                        value = value[:-1]
                    row[field_name] = value

            ann_type = (row.get('type') or '').strip().lower()
            row['type'] = ann_type
            if not ann_type:
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: empty "type", annotation ignored.'
                )
                summary.unimportable_annotations += 1
                continue
            if ann_type == 'chapter' and not (row.get('title') or '').strip():
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: a "chapter" annotation requires a '
                    '"title", annotation ignored.'
                )
                summary.unimportable_annotations += 1
                continue

            time_value = (row.get('time') or '').strip() or '0'
            if not time_value.isdigit():
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: invalid "time" "{time_value}" reset to 0 '
                    '(expected an integer number of milliseconds).'
                )
                time_value = '0'
            row['time'] = time_value

            attachment = (row.get('attachment') or '').strip().strip('/')
            if attachment:
                attachment = attachment.removeprefix(ANNOTATIONS_DIRNAME + '/')
            if ann_type == 'chapter' and attachment:
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment cannot be linked to a "chapter" '
                    'annotation, attachment ignored.'
                )
                attachment = ''
            elif ann_type == 'slide' and not attachment:
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment is required for "slide" '
                    'annotations, annotation ignored.'
                )
                summary.unimportable_annotations += 1
                continue
            elif attachment and not (annotations_dir / attachment).is_file():
                report.warning(
                    f'{ANNOTATIONS_FILENAME}:{line}: attachment "{attachment}" does not exist in '
                    'the annotations directory, annotation ignored.'
                )
                summary.unimportable_annotations += 1
                continue
            row['attachment'] = attachment

            group.annotations.append(row)


def validate_media_integrity(
    groups: dict[str, MediaGroup],
    report: Report,
    summary: Summary,
    ffprobe: str = 'ffprobe',
) -> None:
    """
    Run ``ffprobe`` on every video and audio file of every media group.

    A media with at least one corrupted file is dropped and counted; a missing ``ffprobe``
    executable is a fatal error since no media could be checked.
    """
    corrupted: list[str] = []
    for source_id, group in groups.items():
        files = [group.main_file, *group.extra_streams.values(), *group.audio_tracks.values()]
        for path in files:
            error = probe_media(path, ffprobe=ffprobe)
            if error:
                if 'command not found' in error:
                    report.error(error)
                    return
                report.warning(f'Invalid media file "{path}", media skipped: {error}')
                corrupted.append(source_id)
                break
    for source_id in corrupted:
        del groups[source_id]
        summary.drop_media(source_id, UNIMPORTABLE_CORRUPTED)


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
    summary: Summary,
    apply: bool = False,
) -> None:
    """
    Run the server-side audit checks (slugs, annotation types, speakers, channels).

    Annotations referencing an unknown type are dropped, media targeting ``mscspeaker``
    without a ``speaker_email`` are dropped, and media with an unresolvable explicit channel
    fall back to the folder-based channel. When ``apply`` is true, missing speaker users are
    created and granted the personal-channel permission; otherwise the audit only reports
    what would be done.
    """
    try:
        ngc.check_server()
    except Exception as err:
        report.error(f'Cannot reach the Nudgis server: {err}')
        return

    # Check permissions of the API key.
    try:
        response = ngc.api('users/me/')
    except NudgisRequestError as err:
        report.error(f'Unexpected response from the server when testing the API: {err}')
        return
    else:
        if not response.get('user', {}).get('permissions', {}).get('can_change_users'):
            report.error('The user account of the given API key does not have the required permissions.')
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

    # Annotation types existence: drop the annotations referencing an unknown type.
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
            missing_types = used_types - server_types
            for ann_type in sorted(missing_types):
                report.warning(
                    f'Annotation type "{ann_type}" does not exist on the server; related '
                    'annotations were dropped.'
                )
            if missing_types:
                for group in groups.values():
                    kept = [
                        row for row in group.annotations
                        if (row.get('type') or '').strip() not in missing_types
                    ]
                    summary.unimportable_annotations += len(group.annotations) - len(kept)
                    group.annotations = kept

    # Speaker resolution for "mscspeaker" destinations: a media without any speaker cannot
    # be imported; missing users and permissions are reconciled (or reported) below.
    speaker_emails: set[str] = set()
    no_speaker: list[str] = []
    for source_id, group in groups.items():
        row = group.metadata
        if row and (row.get('channel') or '').strip() == 'mscspeaker':
            emails = _split_pipe(row.get('speaker_email') or '')
            if not emails:
                report.warning(
                    f'Media "{source_id}" targets "mscspeaker" but has no "speaker_email", '
                    'media skipped.'
                )
                no_speaker.append(source_id)
            else:
                speaker_emails.update(emails)
    for source_id in no_speaker:
        del groups[source_id]
        summary.drop_media(source_id, UNIMPORTABLE_NO_SPEAKER)

    for email in sorted(speaker_emails):
        try:
            response = ngc.api('users/', params={'search': email, 'limit': 1})
        except NudgisRequestError as err:
            report.warning(f'Could not check speaker "{email}": {err}')
            continue
        users = response.get('users') or []
        user_id = users[0].get('id') if users else None
        if user_id is None:
            if not apply:
                report.warning(
                    f'Speaker "{email}" does not exist yet; it would be created on import.'
                )
                continue
            try:
                created = ngc.api(
                    'users/add/', method='post', data={'username': email, 'email': email}
                )
            except NudgisRequestError as err:
                report.warning(f'Could not create speaker "{email}": {err}')
                continue
            user_id = created.get('id')
            logger.info('Created Nudgis user "%s" (id %s).', email, user_id)
            if user_id is None:
                continue
        ensure_personal_channel(ngc, user_id, email, report, apply)

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
            report.warning(
                f'Channel "{channel}" for media "{group.source_id}" could not be resolved '
                f'({err}); falling back to the folder-based channel.'
            )
            group.metadata['channel'] = ''

    # Existence of audio tracks (to skip already imported audio tracks).
    for group in groups.values():
        if not group.object_id:
            continue
        try:
            response = ngc.api('medias/audio/tracks/list/', params={'oid': group.object_id})
        except NudgisRequestError as err:
            if err.status_code == 404:
                continue
            report.warning(
                f'Could not list audio tracks of media "{group.source_id}" ({err}); already '
                'imported audio tracks may be re-added.'
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
            report.warning(
                f'Could not list subtitles of media "{group.source_id}" ({err}); already '
                'imported subtitles may be re-added.'
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
            report.warning(
                f'Could not list annotations of media "{group.source_id}" ({err}); already '
                'imported annotations may be re-added.'
            )
        else:
            type_by_id = {t['id']: t['slug'] for t in response.get('types', {}).values()}
            for annotation in response.get('annotations', []):
                type_slug = type_by_id[annotation['type_id']]
                group.existing_elements.append(f'annotation:{type_slug}:{annotation["time"]}')


def ensure_personal_channel(
    ngc: NudgisClient,
    user_id: str,
    email: str,
    report: Report,
    apply: bool,
) -> None:
    """
    Make sure a speaker user is allowed to own a personal channel.

    When ``apply`` is true the permission is granted if needed; otherwise the audit only
    reports that it would be granted.
    """
    try:
        response = ngc.api('perms/get/', params={'type': 'user', 'id': user_id})
    except NudgisRequestError as err:
        report.warning(f'Could not check permissions of speaker "{email}": {err}')
        return
    perm = (response.get('global_permissions') or {}).get('can_have_personal_channel') or {}
    if perm.get('val') or perm.get('inherit_val'):
        return
    if not apply:
        report.warning(
            f'Speaker "{email}" cannot own a personal channel; the permission would be '
            'granted on import.'
        )
        return
    try:
        ngc.api(
            'perms/edit/',
            method='post',
            data={'type': 'user', 'id': user_id, 'can_have_personal_channel': 'True'},
        )
        logger.info('Granted the personal-channel permission to speaker "%s".', email)
    except NudgisRequestError as err:
        report.warning(f'Could not grant the personal-channel permission to "{email}": {err}')


def import_media(
    ngc: NudgisClient,
    groups: dict[str, MediaGroup],
    main_channel: str,
    source_dir: Path,
    mapping_file: Path,
    temp_dir: Path,
    summary: Summary,
    ffmpeg: str = 'ffmpeg',
    ffprobe: str = 'ffprobe',
) -> dict[str, str]:
    """
    Import every media group, fill in ``summary`` and return the ``source_id`` -> ``oid`` map.

    A media is counted as imported (and recorded in the mapping) as soon as its ``add_media``
    step succeeds; a media that cannot be created is counted as an import failure and skipped.
    Each linked element (audio track, subtitle, annotation) is then attached independently and
    accounted for as already existing, imported or failed, so a single element failure does
    not prevent the others from being attached.
    """
    failures: dict[str, str] = {}
    mapping: dict[str, str] = {}
    for source_id, group in groups.items():
        merged = False
        # Media creation: a failure here means the media itself could not be imported.
        try:
            channel = resolve_channel(main_channel, group.rel_dir, group.metadata)
            metadata = build_media_metadata(group.metadata)
            metadata['external_ref'] = group.external_ref
            existing = bool(group.object_id)
            if existing:
                oid = group.object_id
                logger.info('Media "%s" already exists with oid "%s".', source_id, oid)
            else:
                file_path = group.main_file
                if group.extra_streams:
                    # Combine the multi-stream files into a single side-by-side video.
                    merged = True
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
                if metadata.get('slug') and response['slug'] != metadata['slug']:
                    logger.warning(
                        'Media "%s" "%s" did not receive the requested slug "%s", it got "%s".',
                        source_id, oid, metadata['slug'], response['slug']
                    )
                if file_path != group.main_file:
                    # The composed video and its layout preset have been uploaded; clean up.
                    file_path.unlink(missing_ok=True)
                    file_path.with_suffix('.json').unlink(missing_ok=True)
                    logger.debug('Removed temporary composed files for "%s".', source_id)
        except (NudgisRequestError, RuntimeError) as err:
            logger.error('Failed to import media "%s": %s', source_id, err)
            failures[source_id] = str(err)
            summary.import_failures += 1
            continue

        # The media now exists on the server: record it as imported (or already existing).
        mapping[source_id] = oid
        if existing:
            summary.medias_existing += 1
        else:
            summary.medias_imported += 1
            if merged:
                summary.merged_videos += 1
            if group.metadata is not None:
                summary.metadata_applied += 1

        for lang, path in group.audio_tracks.items():
            if f'audio:{lang}' in group.existing_elements:
                logger.info('The "%s" audio track already exists in "%s".', lang, oid)
                summary.audio_tracks_existing += 1
                continue
            try:
                logger.info('Adding "%s" audio track to "%s".', lang, oid)
                with path.open('rb') as fileobj:
                    # Known limitation: the audio track must have a duration close to the video duration
                    ngc.api(
                        'medias/audio/tracks/add/',
                        method='post',
                        data={'oid': oid, 'lang': lang},
                        files={'file': (path.name, fileobj)},
                    )
                summary.audio_tracks_imported += 1
            except (NudgisRequestError, RuntimeError) as err:
                logger.error('Failed to add "%s" audio track to "%s": %s', lang, oid, err)
                summary.audio_tracks_failed += 1

        for lang, path in group.subtitles.items():
            if f'subtitle:{lang}' in group.existing_elements:
                logger.info('The "%s" subtitles already exists in "%s".', lang, oid)
                summary.subtitles_existing += 1
                continue
            try:
                logger.info('Adding "%s" subtitles to "%s".', lang, oid)
                with path.open('rb') as fileobj:
                    ngc.api(
                        'subtitles/add/',
                        method='post',
                        data={'oid': oid, 'lang': lang},
                        files={'file': (path.name, fileobj)},
                    )
                summary.subtitles_imported += 1
            except (NudgisRequestError, RuntimeError) as err:
                logger.error('Failed to add "%s" subtitles to "%s": %s', lang, oid, err)
                summary.subtitles_failed += 1

        for row in group.annotations:
            if 'time' not in row or 'type' not in row:
                continue
            if f'annotation:{row["type"]}:{row["time"]}' in group.existing_elements:
                logger.info(
                    'The "%s" annotation at time "%s" already exists in "%s".',
                    row['type'], row['time'], oid,
                )
                summary.annotations_existing += 1
                continue
            try:
                post_annotation(ngc, oid, row, source_dir)
                summary.annotations_imported += 1
            except (NudgisRequestError, RuntimeError) as err:
                logger.error(
                    'Failed to add "%s" annotation at time "%s" to "%s": %s',
                    row['type'], row['time'], oid, err,
                )
                summary.annotations_failed += 1

    mapping_file.write_text(
        'source_id,oid\n' + ''.join(f'{src},{oid}\n' for src, oid in mapping.items()),
    )
    logger.info('Wrote mapping of %s media to "%s".', len(mapping), mapping_file)

    if failures:
        logger.warning(
            'List of media that could not be created:\n  - %s',
            '\n  - '.join(f'{src}: {msg}' for src, msg in failures.items()),
        )
    logger.info('Media import complete: %s created, %s failed.', len(mapping), len(failures))
    return mapping


def count_planned(groups: dict[str, MediaGroup], summary: Summary) -> None:
    """
    Fill in the "correctly processed" counters of ``summary`` for a dry-run.

    Nothing is uploaded; the counters reflect what a subsequent ``--apply`` run would do
    with the media that survived the audit.
    """
    for group in groups.values():
        if group.object_id:
            summary.medias_existing += 1
        else:
            summary.medias_imported += 1
            if group.extra_streams:
                summary.merged_videos += 1
            if group.metadata is not None:
                summary.metadata_applied += 1
        for lang in group.audio_tracks:
            if f'audio:{lang}' in group.existing_elements:
                summary.audio_tracks_existing += 1
            else:
                summary.audio_tracks_imported += 1
        for lang in group.subtitles:
            if f'subtitle:{lang}' in group.existing_elements:
                summary.subtitles_existing += 1
            else:
                summary.subtitles_imported += 1
        for row in group.annotations:
            key = f'annotation:{row.get("type")}:{row.get("time")}'
            if key in group.existing_elements:
                summary.annotations_existing += 1
            else:
                summary.annotations_imported += 1


def print_summary(summary: Summary, applied: bool) -> None:
    """Log the final import report built from ``summary``."""
    done = 'imported' if applied else 'to import'
    applied_label = 'applied' if applied else 'to apply'
    total_unimportable = sum(len(ids) for ids in summary.unimportable_media.values())
    lines = [
        'Import report:',
        '',
        'Correctly processed content:',
        f'  - Merged video files: {summary.merged_videos}',
        f'  - Media {done}: {summary.medias_imported}',
        f'  - Media already existing: {summary.medias_existing}',
        f'  - Metadata entries {applied_label}: {summary.metadata_applied}',
    ]
    for label, existing, imported, failed in (
        ('Audio tracks', summary.audio_tracks_existing, summary.audio_tracks_imported,
         summary.audio_tracks_failed),
        ('Subtitles', summary.subtitles_existing, summary.subtitles_imported,
         summary.subtitles_failed),
        ('Annotations', summary.annotations_existing, summary.annotations_imported,
         summary.annotations_failed),
    ):
        lines.append(f'  - {label} {done}: {imported}')
        lines.append(f'  - {label} already existing: {existing}')
        if applied:
            lines.append(f'  - {label} failed: {failed}')
    lines += [
        '',
        'Unprocessable content:',
        f'  - Unimportable media: {total_unimportable}',
    ]
    for reason in sorted(summary.unimportable_media):
        lines.append(f'      - {reason}: {len(summary.unimportable_media[reason])}')
    lines.append(f'  - Invalid metadata entries: {summary.invalid_metadata}')
    lines.append(f'  - Unimportable annotations: {summary.unimportable_annotations}')
    if applied:
        lines.append(f'  - Media that could not be created: {summary.import_failures}')
    logger.info('\n'.join(lines))


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
        # Send both "speaker" and "speaker_name" to Nudgis because the API
        # may change and we want to be future-proof.
        metadata['speaker'] = '|'.join(names)
        metadata['speaker_name'] = '|'.join(names)
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
        help='Main migration channel (a title or an "mscpath-..." identifier). '
             'Important: The main channel cannot be targeted with an object id.',
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

    if args.channel.startswith('mscid-') or re.match(r'^c[A-Za-z0-9]{19}$', args.channel):
        logger.error('The channel cannot be targeted with an object id.')
        return 1

    source_dir = args.source_dir
    if not source_dir.is_dir():
        logger.error('Source directory "%s" does not exist.', source_dir)
        return 1
    ids_to_process = args.ids_to_process.split(',') if args.ids_to_process else None

    ngc = NudgisClient(args.conf, setup_logging=False)
    ngc.conf['TIMEOUT'] = max(600, ngc.conf['TIMEOUT'])

    report, summary, groups = run_audit(
        source_dir, ngc, ids_to_process, ffprobe=args.ffprobe, apply=args.apply
    )

    logger.info('Audit completed.')
    for warning in report.warnings:
        logger.warning(warning)
    for error in report.errors:
        logger.error(error)

    if not report.ok:
        logger.error(
            'Audit failed with %s fatal error(s); aborting.', len(report.errors),
        )
        return 1

    if args.apply:
        import_media(
            ngc,
            groups,
            args.channel,
            source_dir,
            args.mapping_file,
            args.temp_dir,
            summary,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
        )
    else:
        logger.info(
            'Audit succeeded for %s media (dry-run). Re-run with "--apply" to import.',
            len(groups),
        )
        count_planned(groups, summary)

    print_summary(summary, applied=args.apply)
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(mass_import(sys.argv[1:]))
