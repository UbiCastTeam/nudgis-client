import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import examples.mass_import as mi

# -------- helpers


def write(path: Path, content: bytes = b'data') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def group(source_id: str, rel_dir: str = '.', object_id=None) -> mi.MediaGroup:
    return mi.MediaGroup(source_id, object_id, Path(f'{source_id}.mp4'), Path(rel_dir))


def group_with(
    source_id, *, metadata=None, annotations=None, rel_dir='.', object_id=None,
    existing_elements=None,
) -> mi.MediaGroup:
    g = group(source_id, rel_dir, object_id)
    g.metadata = metadata
    g.annotations = annotations or []
    g.existing_elements = existing_elements or []
    return g


def make_client(
    *,
    check_server_exc=None,
    catalog=None,
    catalog_exc=None,
    api_map=None,
    api_exc=None,
):
    client = mi.NudgisClient()
    client._server_version = (12, 3, 0)
    client.check_server = mock.MagicMock(
        side_effect=check_server_exc, return_value={}
    )
    client.get_catalog = mock.MagicMock(
        side_effect=catalog_exc, return_value=catalog if catalog is not None else {}
    )
    client.add_media = mock.MagicMock(
        side_effect=lambda **kwargs: {
            'oid': 'v_' + kwargs.get('external_ref', 'x'),
            'slug': kwargs.get('slug', ''),
        }
    )

    def api(url, **kwargs):
        if api_exc and url in api_exc:
            raise api_exc[url]
        if api_map and url in api_map:
            return api_map[url]
        # By default the API key has the permissions required by server_checks.
        if url == 'users/me/':
            return {'user': {'permissions': {'can_change_users': True}}}
        # Channel creation happens when applying the "channels.csv" metadata.
        if url == 'channels/add/':
            return {'oid': 'c_new'}
        return {}

    client.api = mock.MagicMock(side_effect=api)
    return client


METADATA_HEADER = (
    'source_id,title,slug,description,keywords,categories,language,creation,'
    'speaker_name,speaker_email,company_name,company_url,license_name,license_url,'
    'channel,validated,unlisted,detect_slides'
)
CHANNELS_HEADER = 'path,description,reference'


# -------- scan


def test_scan_basic(tmp_path):
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm1_eng.mp3')
    write(tmp_path / 'sub' / 'm2.mp4')
    write(tmp_path / 'metadata.csv', b'ignored')
    write(tmp_path / 'channels.csv', b'ignored')
    write(tmp_path / 'annotations' / 'annotations.csv', b'ignored')
    write(tmp_path / 'readme.txt')  # unexpected file -> warning

    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)

    assert set(groups) == {'m1', 'm2'}
    assert set(groups['m1'].subtitles) == {'fre'}
    assert set(groups['m1'].audio_tracks) == {'eng'}
    assert groups['m2'].rel_dir == Path('sub')
    assert not report.errors
    # The CSV files of the migration standard are validated separately, not scanned here.
    assert sum('Ignoring unexpected file' in w for w in report.warnings) == 1
    assert any('readme.txt' in w for w in report.warnings)


def test_scan_filename_too_long(tmp_path):
    write(tmp_path / 'ok.mp4')
    # 201 chars: allowed by the filesystem but above MAX_FIELD_LENGTH (200).
    write(tmp_path / ('x' * 197 + '.mp4'))
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)
    # The too-long video is skipped as unimportable, the valid one is kept.
    assert set(groups) == {'ok'}
    assert len(summary.unimportable_media[mi.UNIMPORTABLE_FILENAME]) == 1
    assert any('name too long' in w for w in report.warnings)
    assert not report.errors


def test_scan_filename_too_long_non_video(tmp_path):
    write(tmp_path / 'ok.mp4')
    # A non-video file with a too-long name is simply ignored, not counted as a media.
    write(tmp_path / ('x' * 247 + '.srt'))
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)
    assert set(groups) == {'ok'}
    assert not summary.unimportable_media
    assert any('too-long name' in w for w in report.warnings)
    assert not report.errors


def test_scan_multistream_warnings(tmp_path):
    write(tmp_path / 'multi.mp4')
    write(tmp_path / 'multi_stream2.mp4')
    write(tmp_path / 'multi_stream4.mp4')  # gap -> non-contiguous warning
    write(tmp_path / 'noLang.srt')  # no "_lang" suffix

    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)

    assert set(groups['multi'].extra_streams) == {2, 4}
    assert any('non-contiguous' in w for w in report.warnings)
    assert any('will be combined with ffmpeg' in w for w in report.warnings)
    assert any('naming rule' in w for w in report.warnings)
    assert not report.errors


def test_scan_duplicate_source_id(tmp_path):
    write(tmp_path / 'a' / 'dup.mp4')
    write(tmp_path / 'b' / 'dup.mp4')
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)
    # The first file is kept, the duplicate is skipped as unimportable.
    assert set(groups) == {'dup'}
    assert summary.unimportable_media[mi.UNIMPORTABLE_DUPLICATE] == ['dup']
    assert any('Duplicate source id' in w for w in report.warnings)
    assert not report.errors


def test_scan_multistream_without_main(tmp_path):
    write(tmp_path / 'clip.mp4')
    write(tmp_path / 'clip_stream2.mp4')
    write(tmp_path / 'clip_stream2_stream2.mp4')  # secondary of "clip_stream2", not a main media
    report = mi.Report()
    summary = mi.Summary()
    mi.scan_source_tree(tmp_path, None, report, summary)
    assert any('no main media' in w for w in report.warnings)
    assert not report.errors


def test_scan_multistream_out_of_range(tmp_path):
    write(tmp_path / 'range.mp4')
    write(tmp_path / 'range_stream1.mp4')  # index < 2
    write(tmp_path / 'range_stream7.mp4')  # index > 6
    report = mi.Report()
    summary = mi.Summary()
    mi.scan_source_tree(tmp_path, None, report, summary)
    assert sum('out-of-range' in w for w in report.warnings) == 2
    assert not report.errors


def test_scan_linked_orphan(tmp_path):
    write(tmp_path / 'vid.mp4')
    write(tmp_path / 'orphan_fre.srt')  # base "orphan" has no media
    report = mi.Report()
    summary = mi.Summary()
    mi.scan_source_tree(tmp_path, None, report, summary)
    assert any('no main media' in w for w in report.warnings)
    assert not report.errors


def test_scan_linked_duplicate(tmp_path):
    write(tmp_path / 'vid.mp4')
    write(tmp_path / 'vid_fre.srt')
    write(tmp_path / 'vid_fre.vtt')  # same base + language
    report = mi.Report()
    summary = mi.Summary()
    mi.scan_source_tree(tmp_path, None, report, summary)
    assert any('duplicate subtitle' in w.lower() for w in report.warnings)
    assert not report.errors


def test_scan_empty(tmp_path):
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, None, report, summary)
    assert groups == {}
    assert any('No media file to import found' in err for err in report.errors)


def test_scan_ids_to_process(tmp_path):
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm2.mp4')
    write(tmp_path / 'm2_fre.srt')
    write(tmp_path / 'm2_stream2.mp4')
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, ['m1'], report, summary)
    assert set(groups) == {'m1'}
    assert set(groups['m1'].subtitles) == {'fre'}
    assert not report.errors


def test_scan_ids_to_process_prefix_collisions(tmp_path):
    # Files sharing a prefix with an id_to_process but not exact matches exercise
    # the exact-match guards for main files, secondary streams and linked files.
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1extra.mp4')
    write(tmp_path / 'm1extra_stream2.mp4')
    write(tmp_path / 'm1extra_fre.srt')
    report = mi.Report()
    summary = mi.Summary()
    groups = mi.scan_source_tree(tmp_path, ['m1'], report, summary)
    assert set(groups) == {'m1'}
    assert not groups['m1'].extra_streams
    assert not report.errors


# -------- metadata


def test_metadata_missing_file(tmp_path):
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(tmp_path / 'metadata.csv', groups, None, report, summary)
    assert groups['m1'].metadata is None
    assert any('No "metadata.csv"' in w for w in report.warnings)


def test_metadata_missing_columns(tmp_path):
    path = write(tmp_path / 'metadata.csv', b'foo,bar\n1,2\n')
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert groups['m1'].metadata is None
    # A structurally broken CSV is a fatal error.
    assert any('missing mandatory columns' in err for err in report.errors)


def test_metadata_unknown_column(tmp_path):
    path = write(tmp_path / 'metadata.csv', b'source_id,title,weird\nm1,Title,x\n')
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_metadata_csv(path, {'m1': group('m1')}, None, report, summary)
    assert any('unknown column "weird"' in w for w in report.warnings)


def test_metadata_valid(tmp_path):
    content = (
        METADATA_HEADER + '\n'
        'm1,"Title 1",my-slug,desc,a|b,c1|c2,fre,2026-02-26T17:10:00,'
        'Jane|John,jane@x|john@x,Comp,http://c,Lic,http://l,,yes,no,no\n'
    )
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert groups['m1'].metadata is not None
    assert not report.errors
    assert summary.invalid_metadata == 0


def test_metadata_invalid_rows_dropped(tmp_path):
    # Rows that cannot be tied to a media are dropped and counted as invalid entries.
    content = (
        'source_id,title,slug,language,creation,speaker_name,speaker_email,validated\n'
        ',Title,,,,,,\n'  # empty source_id
        'good,Title,goodslug,fre,2026-01-01T00:00:00,,,yes\n'  # valid row (slug recorded)
        'good,Title,other,fre,,,,\n'  # duplicate source_id
        'ghost,Title,,,,,,\n'  # source_id not in groups
    )
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    groups = {'good': group('good')}
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert not report.errors
    assert summary.invalid_metadata == 3
    assert groups['good'].metadata['slug'] == 'goodslug'


def test_metadata_invalid_fields_cleaned(tmp_path):
    # Invalid optional fields are cleaned in place so the media is still importable.
    content = (
        'source_id,title,slug,language,creation,speaker_name,speaker_email,validated\n'
        'm2,,Bad Slug,xx,not-a-date,A|B,a@x,maybe\n'  # title/slug/lang/date/speaker/validated
        'm3,Title,goodslug,,,,,\n'  # slug recorded on m3
        'm4,Title,goodslug,,,,,\n'  # duplicate slug -> cleaned
    )
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    groups = {'m2': group('m2'), 'm3': group('m3'), 'm4': group('m4')}
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert not report.errors
    assert summary.invalid_metadata == 0
    m2 = groups['m2'].metadata
    assert m2['title'] == ''
    assert m2['slug'] == ''
    assert m2['language'] == ''
    assert m2['creation'] == ''
    assert m2['speaker_name'] == '' and m2['speaker_email'] == ''
    assert m2['validated'] == ''
    assert groups['m3'].metadata['slug'] == 'goodslug'
    assert groups['m4'].metadata['slug'] == ''  # duplicate slug dropped
    warnings = '\n'.join(report.warnings)
    assert 'invalid "slug"' in warnings
    assert 'invalid "language"' in warnings
    assert 'invalid "creation"' in warnings
    assert 'different number of values' in warnings
    assert 'invalid "validated"' in warnings
    assert 'already used' in warnings


def test_metadata_too_long_field(tmp_path):
    long_title = 'T' * 300
    path = write(
        tmp_path / 'metadata.csv',
        (METADATA_HEADER + f'\nm1,{long_title},,,,,,,,,,,,,,,,\n').encode('utf-8'),
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert any('value too long' in w for w in report.warnings)
    assert len(groups['m1'].metadata['title']) == mi.MAX_FIELD_LENGTH
    assert not report.errors


@pytest.mark.parametrize('prefix', ['mscspeaker-', 'mscpath-'])
def test_metadata_too_long_channel_title(tmp_path, prefix):
    # An over long channel title is truncated so that the media stays close to where it
    # belongs, instead of being sent to a completely different channel.
    row = ['m1', 'Title'] + [''] * 12 + [f'{prefix}Courses/' + 'T' * 300] + [''] * 3
    path = write(
        tmp_path / 'metadata.csv',
        (METADATA_HEADER + '\n' + ','.join(row) + '\n').encode('utf-8'),
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report, summary)
    assert any('channel title' in w and 'truncated' in w for w in report.warnings)
    assert groups['m1'].metadata['channel'] == f'{prefix}Courses/' + 'T' * mi.MAX_FIELD_LENGTH
    assert not report.errors


def test_metadata_media_without_row(tmp_path):
    path = write(
        tmp_path / 'metadata.csv', (METADATA_HEADER + '\nm1,Title,,,,,,,,,,,,,,,,\n').encode()
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_metadata_csv(path, {'m1': group('m1'), 'm2': group('m2')}, None, report, summary)
    assert any('Media "m2" has no row' in w for w in report.warnings)


def test_metadata_csv_ids_to_process(tmp_path):
    content = METADATA_HEADER + '\nm1,Title 1,,,,,,,,,,,,,,,,\nm2,Title 2,,,,,,,,,,,,,,,,\n'
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    groups = {'m1': group('m1')}
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_metadata_csv(path, groups, ['m1'], report, summary)
    assert groups['m1'].metadata is not None
    assert not report.errors
    assert summary.invalid_metadata == 0


# -------- channels


def test_channels_missing_file(tmp_path):
    report = mi.Report()
    summary = mi.Summary()
    assert mi.validate_channels_csv(tmp_path / 'channels.csv', report, summary) == []
    assert any('No "channels.csv"' in w for w in report.warnings)
    assert not report.errors


def test_channels_missing_columns(tmp_path):
    path = write(tmp_path / 'channels.csv', b'foo,bar\n1,2\n')
    report = mi.Report()
    summary = mi.Summary()
    assert mi.validate_channels_csv(path, report, summary) == []
    # A structurally broken CSV is a fatal error.
    assert any('missing mandatory columns' in err for err in report.errors)


def test_channels_unknown_column(tmp_path):
    path = write(tmp_path / 'channels.csv', b'path,description,weird\nCourse A,Desc,x\n')
    report = mi.Report()
    summary = mi.Summary()
    channels = mi.validate_channels_csv(path, report, summary)
    assert len(channels) == 1
    assert any('unknown column "weird"' in w for w in report.warnings)


def test_channels_valid(tmp_path):
    content = (
        CHANNELS_HEADER + '\n'
        '"Course A/Year 1","<p>My description</p>",lti:moodle.example.local:245\n'
        ' Course B / Year 2 ,,lti:moodle.example.local:246\n'  # surrounding spaces are stripped
        '/Course C/,Only a description,\n'  # leading and trailing separators are stripped
    )
    path = write(tmp_path / 'channels.csv', content.encode('utf-8'))
    report = mi.Report()
    summary = mi.Summary()
    channels = mi.validate_channels_csv(path, report, summary)
    assert [channel.path for channel in channels] == [
        ['Course A', 'Year 1'], ['Course B', 'Year 2'], ['Course C'],
    ]
    assert channels[0].description == '<p>My description</p>'
    assert channels[0].reference == 'lti:moodle.example.local:245'
    assert channels[0].display_path == 'Course A/Year 1'
    assert channels[1].description == ''
    assert channels[2].reference == ''
    assert not report.errors
    assert summary.invalid_channels == 0


def test_channels_invalid_rows_dropped(tmp_path):
    content = (
        CHANNELS_HEADER + '\n'
        ',Desc,\n'  # empty path
        'Course A//Year 1,Desc,\n'  # empty channel title in the path
        'Course A,Desc,\n'  # valid row
        'Course A,Other desc,\n'  # duplicate path
        'Course B,,\n'  # nothing to apply
    )
    path = write(tmp_path / 'channels.csv', content.encode('utf-8'))
    report = mi.Report()
    summary = mi.Summary()
    channels = mi.validate_channels_csv(path, report, summary)
    assert [channel.path for channel in channels] == [['Course A']]
    assert channels[0].description == 'Desc'
    assert summary.invalid_channels == 4
    assert not report.errors
    warnings = '\n'.join(report.warnings)
    assert 'empty or invalid "path"' in warnings
    assert 'duplicate "path"' in warnings
    assert 'no metadata to apply' in warnings


def test_channels_too_long_title_truncated(tmp_path):
    # An over long channel title is truncated (as in the "channel" column of "metadata.csv")
    # so that the metadata are applied to the channel the media have been imported into.
    long_title = 'T' * (mi.MAX_FIELD_LENGTH + 1)
    content = CHANNELS_HEADER + '\n' + f'Course A/{long_title},Desc,\n'
    path = write(tmp_path / 'channels.csv', content.encode('utf-8'))
    report = mi.Report()
    summary = mi.Summary()
    channels = mi.validate_channels_csv(path, report, summary)
    assert [channel.path for channel in channels] == [
        ['Course A', 'T' * mi.MAX_FIELD_LENGTH],
    ]
    warnings = '\n'.join(report.warnings)
    assert f'longer than {mi.MAX_FIELD_LENGTH} characters, truncated' in warnings
    assert summary.invalid_channels == 0
    assert not report.errors


def test_channels_unusable_reference_dropped(tmp_path):
    long_reference = 'lti:moodle.example.local:' + '1' * mi.MAX_FIELD_LENGTH
    content = (
        CHANNELS_HEADER + '\n'
        f'Course A,Desc,{long_reference}\n'  # too long to be stored
        'Course B,Desc,any free-form value\n'  # the reference format is not constrained
        'Course C,Desc,lti:moodle.example.local:245\n'
        'Course D,Desc,lti:moodle.example.local:245\n'  # reference already used
    )
    path = write(tmp_path / 'channels.csv', content.encode('utf-8'))
    report = mi.Report()
    summary = mi.Summary()
    channels = mi.validate_channels_csv(path, report, summary)
    # The rows are kept (their description can be applied), only the reference is dropped.
    assert [channel.reference for channel in channels] == [
        '', 'any free-form value', 'lti:moodle.example.local:245', '',
    ]
    assert summary.invalid_channels == 0
    assert not report.errors
    warnings = '\n'.join(report.warnings)
    assert '"reference" value too long' in warnings
    assert f'at most {mi.MAX_FIELD_LENGTH} characters' in warnings
    assert 'already used by "Course C"' in warnings


# -------- annotations


def test_annotations_no_dir(tmp_path):
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report, summary)
    assert groups['m1'].annotations == []
    assert not report.errors


def test_annotations_dir_without_csv(tmp_path):
    (tmp_path / 'annotations').mkdir()
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report, summary)
    assert any('No "annotations.csv"' in err for err in report.errors)


def test_annotations_missing_columns(tmp_path):
    write(tmp_path / 'annotations' / 'annotations.csv', b'source_id,time\nm1,1\n')
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report, summary)
    assert any('missing mandatory columns' in err for err in report.errors)


def test_annotations_unknown_column(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,weird\nm1,slide,1,x\n',
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report, summary)
    assert any('unknown column "weird"' in w for w in report.warnings)


def test_annotations_valid(tmp_path):
    write(tmp_path / 'annotations' / 'doc.pdf')
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title,content,keywords,attachment\n'
        b'm1,chapter,1000,Intro,Hello,a|b,\n'
        b'm1,slide,2000,Slide,,,doc.pdf\n',
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report, summary)
    assert len(groups['m1'].annotations) == 2
    assert not report.errors
    assert summary.unimportable_annotations == 0


def test_annotations_csv_ids_to_process(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title\nm1,chapter,1000,Intro\nm2,chapter,2000,Other\n',
    )
    groups = {'m1': group('m1')}
    report = mi.Report()
    summary = mi.Summary()
    mi.validate_annotations_csv(tmp_path, groups, ['m1'], report, summary)
    assert len(groups['m1'].annotations) == 1
    assert not report.errors


def test_annotations_unimportable_dropped(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title,attachment\n'
        b',chapter,1,,\n'  # empty source_id -> dropped
        b'ghost,slide,1,Title,a.pdf\n'  # source_id not in groups -> dropped
        b'm1,,1,Title,\n'  # empty type -> dropped
        b'm1,chapter,1,,\n'  # chapter without title -> dropped
        b'm1,chapter,1,Title,doc.pdf\n'  # chapter with attachment -> cleaned + kept
        b'm1,slide,abc,Title,\n'  # slide without attachment -> dropped
        b'm1,slide,1,Title,missing.pdf\n',  # attachment does not exist -> dropped
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report, summary)
    assert not report.errors
    assert summary.unimportable_annotations == 6
    # Only the cleaned chapter survives, without its (forbidden) attachment.
    assert len(groups['m1'].annotations) == 1
    assert groups['m1'].annotations[0]['type'] == 'chapter'
    assert groups['m1'].annotations[0]['attachment'] == ''


def test_annotations_invalid_time_reset(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title\nm1,chapter,abc,Intro\n',
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report, summary)
    assert not report.errors
    assert groups['m1'].annotations[0]['time'] == '0'
    assert any('reset to 0' in w for w in report.warnings)


def test_annotations_too_long_field(tmp_path):
    long_title = 'T' * 300
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        (f'source_id,type,time,title\nm1,chapter,1000,{long_title}\n').encode('utf-8'),
    )
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report, summary)
    assert any('value too long' in w for w in report.warnings)
    assert len(groups['m1'].annotations[0]['title']) == mi.MAX_FIELD_LENGTH
    assert not report.errors


# -------- ffprobe


def _completed(returncode=0, stdout='', stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_probe_not_found(tmp_path):
    with mock.patch.object(mi.subprocess, 'run', side_effect=FileNotFoundError):
        assert 'not found' in mi.probe_media(tmp_path / 'x.mp4')


def test_probe_failure(tmp_path):
    with mock.patch.object(mi.subprocess, 'run', return_value=_completed(1, stderr='boom')):
        assert 'ffprobe failed' in mi.probe_media(tmp_path / 'x.mp4')


def test_probe_bad_json(tmp_path):
    with mock.patch.object(mi.subprocess, 'run', return_value=_completed(0, stdout='nope')):
        assert 'invalid output' in mi.probe_media(tmp_path / 'x.mp4')


def test_probe_no_streams(tmp_path):
    with mock.patch.object(
        mi.subprocess, 'run', return_value=_completed(0, stdout='{"streams": []}')
    ):
        assert 'no media stream' in mi.probe_media(tmp_path / 'x.mp4')


def test_probe_valid(tmp_path):
    with mock.patch.object(
        mi.subprocess,
        'run',
        return_value=_completed(0, stdout='{"streams": [{"codec_type": "video"}]}'),
    ):
        assert mi.probe_media(tmp_path / 'x.mp4') is None


def test_validate_media_integrity_drops_corrupted():
    g = group('m1')
    g.extra_streams[2] = Path('m1_2.mp4')
    g.audio_tracks['eng'] = Path('m1_eng.mp3')
    report = mi.Report()
    summary = mi.Summary()
    groups = {'m1': g}
    with mock.patch.object(mi, 'probe_media', return_value='bad'):
        mi.validate_media_integrity(groups, report, summary)
    # The whole media is dropped as soon as one of its files is corrupted.
    assert 'm1' not in groups
    assert summary.unimportable_media[mi.UNIMPORTABLE_CORRUPTED] == ['m1']
    assert not report.errors


def test_validate_media_integrity_valid():
    groups = {'m1': group('m1')}
    report = mi.Report()
    summary = mi.Summary()
    with mock.patch.object(mi, 'probe_media', return_value=None):
        mi.validate_media_integrity(groups, report, summary)
    assert set(groups) == {'m1'}
    assert not report.errors
    assert not summary.unimportable_media


def test_validate_media_integrity_ffprobe_missing():
    groups = {'m1': group('m1')}
    report = mi.Report()
    summary = mi.Summary()
    with mock.patch.object(mi, 'probe_media', return_value='"ffprobe" command not found; boom'):
        mi.validate_media_integrity(groups, report, summary)
    # A missing ffprobe is fatal, media are not dropped.
    assert set(groups) == {'m1'}
    assert any('command not found' in err for err in report.errors)
    assert not summary.unimportable_media


# -------- server checks


def request_error(status_code=500):
    return mi.NudgisRequestError('boom', status_code=status_code)


def test_server_unreachable():
    report = mi.Report()
    summary = mi.Summary()
    client = make_client(check_server_exc=RuntimeError('down'))
    mi.server_checks(client, {}, report, summary)
    assert any('Cannot reach' in err for err in report.errors)
    client.get_catalog.assert_not_called()


def test_server_api_permission_missing():
    # An API key whose account lacks the required permission aborts the run.
    client = make_client(
        api_map={'users/me/': {'user': {'permissions': {'can_change_users': False}}}},
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': group_with('a')}, report, summary)
    assert any('does not have the required permissions' in err for err in report.errors)
    client.get_catalog.assert_not_called()


def test_server_api_check_error():
    # A failure while testing the API key aborts the run.
    client = make_client(api_exc={'users/me/': request_error()})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': group_with('a')}, report, summary)
    assert any('testing the API' in err for err in report.errors)
    client.get_catalog.assert_not_called()


def test_server_full():
    groups = {
        'a': group_with(
            'a',
            metadata={'slug': 'taken', 'channel': ''},
            annotations=[{'type': 'custom'}, {'type': 'chapter'}],
        ),
        'b': group_with('b', metadata={'slug': 'free', 'channel': 'mscspeaker',
                                       'speaker_email': 'known@x'}),
        'c': group_with('c', metadata={'channel': 'mscspeaker', 'speaker_email': ''}),
        'd': group_with('d', metadata={'channel': 'mscid-CID'}),  # resolved by oid
        'g': group_with('g', metadata={'channel': 'my-channel'}),  # resolved by slug
        'f': group_with('f', metadata={'channel': 'mscpath-Foo'}),  # auto-created, skipped
    }
    client = make_client(
        catalog={'videos': [{'slug': 'taken', 'oid': 'vexist'}], 'channels': [{}]},
        api_map={
            'annotations/types/list/': {'types': [{'id': 1, 'slug': 'other'}]},
            'users/': {'users': [{'email': 'known@x', 'id': '25508'}]},
            'perms/get/': {'global_permissions': {'can_have_personal_channel': {'val': True}}},
        },
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    warnings = '\n'.join(report.warnings)
    assert not report.errors
    # An already used slug is only a warning (the media may be re-imported).
    assert 'Slug "taken"' in warnings
    assert 'Slug "free"' not in warnings
    # The annotation using an unknown type is dropped, the internal one is kept.
    assert [row['type'] for row in groups['a'].annotations] == ['chapter']
    assert summary.unimportable_annotations == 1
    # A media targeting "mscspeaker" without a speaker email is dropped.
    assert 'c' not in groups
    assert summary.unimportable_media[mi.UNIMPORTABLE_NO_SPEAKER] == ['c']
    # The explicit channels (oid and slug) were looked up on the server.
    channel_calls = [c for c in client.api.call_args_list if c.args[0] == 'channels/get/']
    assert {tuple(c.kwargs['params'].items()) for c in channel_calls} == {
        (('oid', 'CID'),), (('slug', 'my-channel'),),
    }


def test_server_existing_elements():
    # A media already on the server (matched by external ref) has its existing audio
    # tracks, subtitles and annotations collected so they can be skipped on import.
    g = group_with('a')
    client = make_client(
        catalog={'videos': [{'external_ref': 'migration:a', 'oid': 'vexist'}]},
        api_map={
            'medias/audio/tracks/list/': {'audio_tracks': [
                {'language': 'fre', 'is_original': False},
                {'language': 'eng', 'is_original': True},  # original -> skipped
            ]},
            'subtitles/': {'subtitles': [
                {'lang_code': 'fre', 'auto_transcripted': False, 'auto_translated': False},
                {'lang_code': 'eng', 'auto_translated': True},  # auto -> skipped
            ]},
            'annotations/list/': {
                'types': {10: {'id': 10, 'slug': 'chapter'}},
                'annotations': [{'type_id': 10, 'time': 1000}],
            },
        },
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': g}, report, summary)
    assert g.object_id == 'vexist'
    assert g.existing_elements == ['audio:fre', 'subtitle:fre', 'annotation:chapter:1000']
    assert not report.errors


def test_server_existing_elements_not_found():
    g = group_with('a', object_id='vexist')
    err = request_error(status_code=404)
    client = make_client(api_exc={
        'medias/audio/tracks/list/': err,
        'subtitles/': err,
        'annotations/list/': err,
    })
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': g}, report, summary)
    assert g.existing_elements == []
    assert not report.errors


def test_server_existing_elements_error():
    # A listing failure no longer aborts the run: it is a warning and the import proceeds.
    g = group_with('a', object_id='vexist')
    err = request_error(status_code=500)
    client = make_client(api_exc={
        'medias/audio/tracks/list/': err,
        'subtitles/': err,
        'annotations/list/': err,
    })
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': g}, report, summary)
    assert not report.errors
    warnings = '\n'.join(report.warnings)
    assert 'Could not list audio tracks' in warnings
    assert 'Could not list subtitles' in warnings
    assert 'Could not list annotations' in warnings


def test_server_catalog_error():
    client = make_client(catalog_exc=request_error())
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {'a': group_with('a', metadata={'slug': 's'})}, report, summary)
    assert any('check slugs' in w for w in report.warnings)


def test_server_annotation_types_error():
    client = make_client(api_exc={'annotations/types/list/': request_error()})
    report = mi.Report()
    summary = mi.Summary()
    groups = {'a': group_with('a', annotations=[{'type': 'custom'}])}
    mi.server_checks(client, groups, report, summary)
    assert any('annotation types' in w for w in report.warnings)
    # The annotations are kept when the types cannot be checked.
    assert len(groups['a'].annotations) == 1


def test_server_speaker_user_missing():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_map={'users/': {'users': []}})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    # In audit mode the missing user is only reported, not created.
    assert any('does not exist yet' in w for w in report.warnings)
    assert not report.errors
    assert not any(c.args[0] == 'users/add/' for c in client.api.call_args_list)


def test_server_speaker_user_created():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_map={
        'users/': {'users': []},
        'users/add/': {'id': '77'},
        'perms/get/': {'global_permissions': {'can_have_personal_channel': {'val': False}}},
    })
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary, apply=True)
    urls = [c.args[0] for c in client.api.call_args_list]
    # The missing user is created and granted the personal-channel permission.
    assert 'users/add/' in urls
    assert 'perms/edit/' in urls
    assert not report.errors


def test_server_speaker_permission_already_granted():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'known@x'})}
    client = make_client(api_map={
        'users/': {'users': [{'email': 'known@x', 'id': '25508'}]},
        'perms/get/': {'global_permissions': {'can_have_personal_channel': {'inherit_val': True}}},
    })
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary, apply=True)
    urls = [c.args[0] for c in client.api.call_args_list]
    assert 'perms/edit/' not in urls
    assert not report.errors


def test_server_speaker_create_error():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(
        api_map={'users/': {'users': []}},
        api_exc={'users/add/': request_error()},
    )
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary, apply=True)
    assert any('Could not create speaker' in w for w in report.warnings)
    # No permission is checked when the user could not be created.
    assert not any(c.args[0] == 'perms/get/' for c in client.api.call_args_list)


def test_server_speaker_create_without_id():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_map={'users/': {'users': []}, 'users/add/': {}})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary, apply=True)
    # The created user has no id, so no permission is checked.
    assert not any(c.args[0] == 'perms/get/' for c in client.api.call_args_list)
    assert not report.errors


def test_server_speaker_user_error():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_exc={'users/': request_error()})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    assert any('Could not check speaker' in w for w in report.warnings)


def test_server_speaker_subchannel_target():
    # A personal sub-channel target resolves the speaker like "mscspeaker" does, and is not
    # looked up as an explicit channel (it is resolved during the import).
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker-Courses/2026',
                                             'speaker_email': 'known@x'})}
    client = make_client(api_map={
        'users/': {'users': [{'email': 'known@x', 'id': '25508'}]},
        'perms/get/': {'global_permissions': {'can_have_personal_channel': {'val': True}}},
    })
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    urls = [c.args[0] for c in client.api.call_args_list]
    assert 'users/' in urls
    assert 'channels/get/' not in urls
    assert groups['b'].metadata['channel'] == 'mscspeaker-Courses/2026'
    assert not report.errors


def test_server_speaker_subchannel_without_email():
    # As for "mscspeaker", a personal sub-channel target without recipient is not importable.
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker-Courses',
                                             'speaker_email': ''})}
    client = make_client()
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    assert 'b' not in groups
    assert summary.unimportable_media[mi.UNIMPORTABLE_NO_SPEAKER] == ['b']


def test_server_channel_missing():
    groups = {'d': group_with('d', metadata={'channel': 'mscid-CID'})}
    client = make_client(api_exc={'channels/get/': request_error()})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, groups, report, summary)
    # An unresolvable channel is cleaned so the media falls back to the folder channel.
    assert any('could not be resolved' in w for w in report.warnings)
    assert groups['d'].metadata['channel'] == ''
    assert not report.errors


def test_server_checks_channels():
    # The "channels.csv" entries are resolved against the catalog to report the channels that
    # do not exist yet and the references already used by another channel.
    existing = mi.ChannelUpdate(path=['Migration', 'Course A'], description='D')
    missing = mi.ChannelUpdate(path=['Migration', 'Course B'], reference='lti:moodle:1')
    taken = mi.ChannelUpdate(path=['Migration'], reference='lti:moodle:2')
    client = make_client(catalog={'channels': [
        {'oid': 'c1', 'title': 'Migration', 'parent_oid': None},
        {'oid': 'c2', 'title': 'Course A', 'parent_oid': 'c1'},
        {'oid': 'c3', 'title': 'Other', 'parent_oid': None, 'external_ref': 'lti:moodle:2'},
    ]})
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {}, report, summary, channels=[existing, missing, taken])
    assert existing.object_id == 'c2'
    assert missing.object_id is None
    assert taken.object_id == 'c1'
    assert not report.errors
    warnings = '\n'.join(report.warnings)
    assert 'Channel "Migration/Course B" does not exist yet' in warnings
    assert 'Channel "Migration/Course A" does not exist yet' not in warnings
    assert 'Reference "lti:moodle:2" (channel "Migration") is already used' in warnings
    assert 'Reference "lti:moodle:1"' not in warnings


def test_server_checks_channels_catalog_error():
    # Without the catalog the channels cannot be resolved, but the audit still succeeds.
    update = mi.ChannelUpdate(path=['Migration'], description='D')
    client = make_client(catalog_exc=request_error())
    report = mi.Report()
    summary = mi.Summary()
    mi.server_checks(client, {}, report, summary, channels=[update])
    assert update.object_id is None
    assert not report.errors
    assert any('check slugs and channels' in w for w in report.warnings)


# -------- ensure_personal_channel


def test_ensure_personal_channel_perms_error():
    client = make_client(api_exc={'perms/get/': request_error()})
    report = mi.Report()
    mi.ensure_personal_channel(client, '25508', 'x@x', report, apply=True)
    assert any('Could not check permissions' in w for w in report.warnings)


def test_ensure_personal_channel_audit_reports():
    client = make_client(api_map={
        'perms/get/': {'global_permissions': {'can_have_personal_channel': {'val': False}}},
    })
    report = mi.Report()
    mi.ensure_personal_channel(client, '25508', 'x@x', report, apply=False)
    # In audit mode the missing permission is only reported, not granted.
    assert any('cannot own a personal channel' in w for w in report.warnings)
    assert not any(c.args[0] == 'perms/edit/' for c in client.api.call_args_list)


def test_ensure_personal_channel_grant_error():
    client = make_client(
        api_map={'perms/get/': {'global_permissions': {'can_have_personal_channel': {'val': False}}}},
        api_exc={'perms/edit/': request_error()},
    )
    report = mi.Report()
    mi.ensure_personal_channel(client, '25508', 'x@x', report, apply=True)
    assert any('Could not grant' in w for w in report.warnings)


# -------- mapping helpers


def test_build_media_metadata_full():
    row = {
        'title': 'T', 'slug': 's', 'description': 'd', 'language': 'fre',
        'creation': '2026-01-01T00:00:00', 'company_name': 'C', 'company_url': 'cu',
        'license_name': 'L', 'license_url': 'lu', 'validated': 'yes', 'unlisted': 'no',
        'detect_slides': 'no', 'keywords': 'a|b', 'categories': 'c1|c2',
        'speaker_name': 'N1|N2', 'speaker_email': 'e1|e2',
    }
    metadata = mi.build_media_metadata(row)
    assert metadata == {
        'title': 'T', 'slug': 's', 'description': 'd',
        'language': 'fre', 'creation': '2026-01-01T00:00:00', 'company': 'C',
        'company_url': 'cu', 'license': 'L', 'license_url': 'lu', 'validated': 'yes',
        'unlisted': 'no', 'detect_slides': 'no', 'keywords': 'a,b', 'category': 'c1\nc2',
        'speaker': 'N1|N2', 'speaker_name': 'N1|N2', 'speaker_email': 'e1|e2',
    }


def test_build_media_metadata_no_row():
    assert mi.build_media_metadata(None) == {}


@pytest.mark.parametrize('channel, expected', [
    ('mscpath-A/B', 'mscpath-A/B'),
    ('mscpath-A/' + 'B' * 300, 'mscpath-A/' + 'B' * mi.MAX_FIELD_LENGTH),
    ('mscspeaker-' + 'C' * 201 + '/D', 'mscspeaker-' + 'C' * mi.MAX_FIELD_LENGTH + '/D'),
    # Targets without a path are left untouched.
    ('mscspeaker', 'mscspeaker'),
    ('mscid-' + 'x' * 300, 'mscid-' + 'x' * 300),
    ('a-slug', 'a-slug'),
])
def test_truncate_channel_titles(channel, expected):
    assert mi.truncate_channel_titles(channel) == expected


@pytest.mark.parametrize('channel, expected', [
    ('mscspeaker', []),
    ('mscspeaker-Top', ['Top']),
    ('mscspeaker-Top channel/Mid channel/Sub channel',
     ['Top channel', 'Mid channel', 'Sub channel']),
    ('mscspeaker- Top / Sub /', ['Top', 'Sub']),  # empty and padded titles are cleaned up
    ('mscspeaker-', []),  # an empty path targets the personal channel itself
    ('mscspeakers', None),
    ('mscpath-Top/Sub', None),
    ('', None),
])
def test_parse_speaker_path(channel, expected):
    assert mi.parse_speaker_path(channel) == expected


@pytest.mark.parametrize('main, rel_dir, row, expected', [
    ('Migration', '.', {'channel': 'mscid-x'}, 'mscid-x'),
    # A personal channel target is kept as is, it is resolved during the import.
    ('Migration', '.', {'channel': 'mscspeaker-Sub'}, 'mscspeaker-Sub'),
    ('Migration', 'sub/deep', {'channel': ''}, 'mscpath-Migration/sub/deep'),
    ('mscpath-Root', 'a', None, 'mscpath-Root/a'),
    ('Migration', '.', None, 'mscpath-Migration'),
])
def test_resolve_channel(main, rel_dir, row, expected):
    assert mi.resolve_channel(main, Path(rel_dir), row) == expected


def test_post_annotation_default_content(tmp_path):
    # A non-internal annotation type with no content gets a placeholder content.
    client = make_client()
    row = {'type': 'custom', 'time': '1000', 'title': '', 'content': '',
           'keywords': '', 'attachment': ''}
    mi.post_annotation(client, 'oid1', row, tmp_path)
    _, kwargs = client.api.call_args
    assert kwargs['data']['type_slug'] == 'custom'
    assert kwargs['data']['content'] == '-'


# -------- import


def test_import_media(tmp_path):
    write(tmp_path / 'm1.mp4')
    sub_path = write(tmp_path / 'm1_fre.srt')
    audio_path = write(tmp_path / 'm1_eng.mp3')
    write(tmp_path / 'annotations' / 'doc.pdf')

    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.subtitles['fre'] = sub_path
    g1.audio_tracks['eng'] = audio_path
    g1.metadata = {'title': 'Title 1', 'channel': ''}
    g1.annotations = [
        {'type': 'chapter', 'time': '1000', 'title': 'Intro', 'content': 'Hi',
         'keywords': 'a|b', 'attachment': ''},
        {'type': 'slide', 'time': '2000', 'title': 'Slide', 'content': '',
         'keywords': '', 'attachment': 'doc.pdf'},
    ]
    groups = {'m1': g1}

    mapping_file = tmp_path / 'mapping.csv'
    client = make_client()
    summary = mi.Summary()

    mapping = mi.import_media(
        client, groups, 'Migration', tmp_path, mapping_file, tmp_path / 'temp', summary
    )

    assert mapping == {'m1': 'v_migration:m1'}
    client.add_media.assert_called_once()
    _, kwargs = client.add_media.call_args
    assert kwargs['title'] == 'Title 1'
    assert kwargs['channel'] == 'mscpath-Migration'
    assert kwargs['external_ref'] == 'migration:m1'
    assert kwargs['origin'] == 'migration:m1'
    called_urls = [call.args[0] for call in client.api.call_args_list]
    assert 'subtitles/add/' in called_urls
    assert 'medias/audio/tracks/add/' in called_urls
    assert called_urls.count('annotations/post/') == 2
    assert mapping_file.read_text() == 'source_id,oid\nm1,v_migration:m1\n'
    assert summary.medias_imported == 1
    assert summary.medias_existing == 0
    assert summary.metadata_applied == 1
    assert summary.audio_tracks_imported == 1
    assert summary.subtitles_imported == 1
    assert summary.annotations_imported == 2
    assert summary.annotations_existing == 0
    assert summary.annotations_failed == 0


def test_import_media_slug_mismatch(tmp_path, caplog):
    # The server may assign a different slug than requested; this is only warned about.
    write(tmp_path / 'm1.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.metadata = {'title': 'T', 'slug': 'wanted', 'channel': ''}
    client = make_client()
    client.add_media = mock.MagicMock(side_effect=lambda **_kw: {'oid': 'v1', 'slug': 'other'})
    summary = mi.Summary()
    with caplog.at_level(logging.WARNING):
        mapping = mi.import_media(
            client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp', summary
        )
    assert mapping == {'m1': 'v1'}
    assert 'did not receive the requested slug' in caplog.text


def test_import_media_already_exists(tmp_path):
    # A media already present on the server (object_id set) is not re-uploaded, and its
    # already-imported elements (listed in existing_elements) are skipped.
    sub_path = write(tmp_path / 'm1_fre.srt')
    audio_path = write(tmp_path / 'm1_eng.mp3')

    g1 = mi.MediaGroup('m1', 'vexist', tmp_path / 'm1.mp4', Path('.'))
    g1.subtitles['fre'] = sub_path
    g1.audio_tracks['eng'] = audio_path
    g1.annotations = [{'type': 'chapter', 'time': '1000', 'title': 'Intro'}]
    g1.existing_elements = ['audio:eng', 'subtitle:fre', 'annotation:chapter:1000']

    mapping_file = tmp_path / 'mapping.csv'
    client = make_client()
    summary = mi.Summary()

    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, mapping_file, tmp_path / 'temp', summary
    )

    assert mapping == {'m1': 'vexist'}
    client.add_media.assert_not_called()
    called_urls = [call.args[0] for call in client.api.call_args_list]
    assert 'subtitles/add/' not in called_urls
    assert 'medias/audio/tracks/add/' not in called_urls
    assert 'annotations/post/' not in called_urls
    assert summary.medias_existing == 1
    assert summary.medias_imported == 0
    assert summary.metadata_applied == 0
    assert summary.audio_tracks_existing == 1
    assert summary.audio_tracks_imported == 0
    assert summary.subtitles_existing == 1
    assert summary.subtitles_imported == 0
    assert summary.annotations_existing == 1
    assert summary.annotations_imported == 0


def test_import_media_without_metadata_row(tmp_path):
    write(tmp_path / 'm1.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('sub'))
    client = make_client()
    summary = mi.Summary()
    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp', summary
    )
    assert mapping == {'m1': 'v_migration:m1'}
    _, kwargs = client.add_media.call_args
    assert kwargs['title'] == 'm1'
    assert kwargs['channel'] == 'mscpath-Migration/sub'
    assert summary.medias_imported == 1
    assert summary.metadata_applied == 0


def speaker_channel_client(created):
    # A client resolving the personal channel of "known@x" and creating the missing levels.
    def api(url, **kwargs):
        if url == 'users/':
            return {'users': [{'email': 'known@x', 'id': '25508'}]}
        if url == 'channels/personal/':
            return {'oid': 'cPerso'}
        if url == 'channels/get/':
            raise request_error(404)
        if url == 'channels/add/':
            created.append((kwargs['data']['title'], kwargs['data']['parent']))
            return {'oid': f'c{len(created)}'}
        return {}

    client = make_client()
    client.api = mock.MagicMock(side_effect=api)
    return client


def test_import_media_speaker_subchannel(tmp_path):
    # Media targeting a sub-channel of a personal channel are uploaded into the resolved oid,
    # and the resolution is shared by every media of the same speaker.
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm2.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.metadata = {'title': 'T1', 'channel': 'mscspeaker-Courses/2026',
                   'speaker_email': 'known@x'}
    g2 = mi.MediaGroup('m2', None, tmp_path / 'm2.mp4', Path('.'))
    g2.metadata = {'title': 'T2', 'channel': 'mscspeaker-Courses/2026',
                   'speaker_email': 'known@x'}
    created = []
    client = speaker_channel_client(created)
    summary = mi.Summary()

    mapping = mi.import_media(
        client, {'m1': g1, 'm2': g2}, 'Migration', tmp_path, tmp_path / 'map.csv',
        tmp_path / 'temp', summary,
    )

    assert mapping == {'m1': 'v_migration:m1', 'm2': 'v_migration:m2'}
    channels = [kwargs['channel'] for _, kwargs in client.add_media.call_args_list]
    assert channels == ['mscid-c2', 'mscid-c2']
    # The channels of the path are created once, not once per media.
    assert created == [('Courses', 'cPerso'), ('2026', 'c1')]
    assert summary.medias_imported == 2
    assert summary.import_failures == 0


def test_import_media_speaker_subchannel_error(tmp_path):
    # A personal sub-channel that cannot be resolved makes its media an import failure only.
    write(tmp_path / 'm1.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.metadata = {'title': 'T1', 'channel': 'mscspeaker-Courses', 'speaker_email': 'ghost@x'}
    client = make_client(api_map={'users/': {'users': []}})
    summary = mi.Summary()
    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp',
        summary,
    )
    assert mapping == {}
    client.add_media.assert_not_called()
    assert summary.import_failures == 1


def test_import_media_continues_on_upload_error(tmp_path):
    # A media whose upload fails is reported but does not stop the following ones.
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm2.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g2 = mi.MediaGroup('m2', None, tmp_path / 'm2.mp4', Path('.'))
    mapping_file = tmp_path / 'mapping.csv'

    client = make_client()
    summary = mi.Summary()

    def add_media(**kwargs):
        if kwargs['external_ref'] == 'migration:m1':
            raise mi.NudgisRequestError('upload failed', status_code=500)
        return {'oid': 'v_' + kwargs['external_ref'], 'slug': ''}

    client.add_media = mock.MagicMock(side_effect=add_media)

    mapping = mi.import_media(
        client, {'m1': g1, 'm2': g2}, 'Migration', tmp_path, mapping_file, tmp_path / 'temp', summary
    )

    # The failed media is excluded from the mapping; the next one is still imported.
    assert mapping == {'m2': 'v_migration:m2'}
    assert client.add_media.call_count == 2
    assert mapping_file.read_text() == 'source_id,oid\nm2,v_migration:m2\n'
    assert summary.import_failures == 1
    assert summary.medias_imported == 1


def test_import_media_continues_on_linked_element_error(tmp_path):
    # A linked-element failure does not prevent the media (and the next ones) from importing.
    write(tmp_path / 'm1.mp4')
    sub_path = write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm2.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.subtitles['fre'] = sub_path
    g2 = mi.MediaGroup('m2', None, tmp_path / 'm2.mp4', Path('.'))

    client = make_client()
    summary = mi.Summary()

    def api(url, **kwargs):
        if url == 'subtitles/add/':
            raise mi.NudgisRequestError('subtitle failed', status_code=500)
        return {}

    client.api = mock.MagicMock(side_effect=api)

    mapping = mi.import_media(
        client, {'m1': g1, 'm2': g2}, 'Migration', tmp_path, tmp_path / 'map.csv',
        tmp_path / 'temp', summary,
    )

    # m1's upload succeeded, so it is counted as imported even though its subtitle failed.
    assert set(mapping) == {'m1', 'm2'}
    assert client.add_media.call_count == 2
    assert summary.import_failures == 0
    assert summary.medias_imported == 2
    assert summary.subtitles_failed == 1
    assert summary.subtitles_imported == 0


def test_import_media_element_failures(tmp_path):
    # Each linked element failure is isolated and counted; the media is still imported.
    write(tmp_path / 'm1.mp4')
    sub_path = write(tmp_path / 'm1_fre.srt')
    audio_path = write(tmp_path / 'm1_eng.mp3')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.subtitles['fre'] = sub_path
    g1.audio_tracks['eng'] = audio_path
    g1.annotations = [{'type': 'chapter', 'time': '1000', 'title': 'Intro'}]

    client = make_client()
    summary = mi.Summary()

    def api(url, **kwargs):
        if url in ('medias/audio/tracks/add/', 'subtitles/add/', 'annotations/post/'):
            raise mi.NudgisRequestError('boom', status_code=500)
        return {}

    client.api = mock.MagicMock(side_effect=api)

    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp', summary
    )

    # The media itself was created, so it is imported despite every element failing.
    assert mapping == {'m1': 'v_migration:m1'}
    assert summary.medias_imported == 1
    assert summary.import_failures == 0
    assert summary.audio_tracks_failed == 1
    assert summary.subtitles_failed == 1
    assert summary.annotations_failed == 1


def test_import_media_skips_malformed_annotation(tmp_path):
    # A row missing its type/time is defensively skipped and not counted.
    write(tmp_path / 'm1.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.annotations = [{'title': 'no type nor time'}]
    client = make_client()
    summary = mi.Summary()
    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp', summary
    )
    assert mapping == {'m1': 'v_migration:m1'}
    called_urls = [call.args[0] for call in client.api.call_args_list]
    assert 'annotations/post/' not in called_urls
    assert summary.annotations_imported == 0
    assert summary.annotations_failed == 0


def test_import_media_multistream(tmp_path):
    # A multi-stream media is combined into a temporary file, uploaded, then cleaned up.
    main = write(tmp_path / 'multi.mp4')
    second = write(tmp_path / 'multi_2.mp4')
    temp_dir = tmp_path / 'temp'
    g = mi.MediaGroup('multi', None, main, Path('.'))
    g.extra_streams[2] = second

    client = make_client()
    summary = mi.Summary()
    captured = {}

    def fake_compose(inputs, output, ffmpeg='ffmpeg', ffprobe='ffprobe'):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'composed')
        # compose_streams writes the layout preset next to the output (see compose_multistream).
        output.with_suffix('.json').write_text('{"composition_area": {"w": 1280, "h": 640}}')
        captured['call'] = (inputs, output, ffmpeg, ffprobe)

    with mock.patch.object(mi, 'compose_streams', side_effect=fake_compose):
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv', temp_dir, summary,
            ffmpeg='/usr/bin/ffmpeg', ffprobe='/usr/bin/ffprobe',
        )

    temp_file = temp_dir / 'multi.mp4'
    assert captured['call'] == ([main, second], temp_file, '/usr/bin/ffmpeg', '/usr/bin/ffprobe')
    _, kwargs = client.add_media.call_args
    assert kwargs['file_path'] == temp_file
    # The layout preset is forwarded to the server as the JSON content written by compose_streams.
    assert kwargs['layout_preset'] == '{"composition_area": {"w": 1280, "h": 640}}'
    assert not temp_file.exists()  # the temporary file is removed after a successful upload
    assert not temp_file.with_suffix('.json').exists()  # the layout preset is cleaned up too
    assert mapping == {'multi': 'v_migration:multi'}
    assert summary.merged_videos == 1
    assert summary.medias_imported == 1


def test_import_media_multistream_skipped_when_exists(tmp_path):
    # An already imported multi-stream media is neither combined nor re-uploaded.
    g = mi.MediaGroup('multi', 'vexist', tmp_path / 'multi.mp4', Path('.'))
    g.extra_streams[2] = tmp_path / 'multi_2.mp4'
    client = make_client()
    summary = mi.Summary()
    with mock.patch.object(mi, 'compose_streams') as compose:
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv',
            tmp_path / 'temp', summary,
        )
    compose.assert_not_called()
    client.add_media.assert_not_called()
    assert mapping == {'multi': 'vexist'}
    assert summary.medias_existing == 1
    assert summary.merged_videos == 0


def test_import_media_multistream_compose_error(tmp_path):
    # A composition failure is reported and does not stop the process.
    main = write(tmp_path / 'multi.mp4')
    g = mi.MediaGroup('multi', None, main, Path('.'))
    g.extra_streams[2] = write(tmp_path / 'multi_2.mp4')
    client = make_client()
    summary = mi.Summary()
    with mock.patch.object(mi, 'compose_streams', side_effect=RuntimeError('ffmpeg failed')):
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv',
            tmp_path / 'temp', summary,
        )
    assert mapping == {}
    client.add_media.assert_not_called()
    assert summary.import_failures == 1
    assert summary.merged_videos == 0


# -------- channels import


def test_build_channel_paths():
    catalog = {'channels': [
        {'oid': 'c1', 'title': 'Migration', 'parent_oid': None},
        {'oid': 'c2', 'title': 'Course A', 'parent_oid': 'c1'},
        {'oid': 'c3', 'title': 'Year 1', 'parent_oid': 'c2'},
        {'oid': 'c4', 'title': 'Course A', 'parent_oid': None},  # same title at the root
        {'title': 'No oid', 'parent_oid': None},  # ignored
    ]}
    assert mi.build_channel_paths(catalog) == {
        'Migration': 'c1',
        'Migration/Course A': 'c2',
        'Migration/Course A/Year 1': 'c3',
        'Course A': 'c4',
    }


def test_build_channel_paths_empty():
    assert mi.build_channel_paths({}) == {}


def test_build_channel_paths_loop():
    # A channel that is its own ancestor must not loop forever.
    catalog = {'channels': [{'oid': 'c1', 'title': 'Loop', 'parent_oid': 'c1'}]}
    assert mi.build_channel_paths(catalog) == {'Loop': 'c1'}


def test_ensure_channel_existing():
    client = make_client()
    paths = {'Migration': 'c1', 'Migration/Course A': 'c2'}
    assert mi.ensure_channel(client, ['Migration', 'Course A'], paths) == 'c2'
    client.api.assert_not_called()


def test_ensure_channel_creates_root():
    client = make_client(api_map={'channels/add/': {'oid': 'cRoot'}})
    paths = {}
    assert mi.ensure_channel(client, ['Root'], paths) == 'cRoot'
    assert paths == {'Root': 'cRoot'}
    _, kwargs = client.api.call_args
    # A channel created at the root of the catalog has no parent.
    assert kwargs['data'] == {'title': 'Root'}


def test_get_or_create_channel_existing():
    client = make_client(api_map={'channels/get/': {'info': {'oid': 'cSub'}}})
    assert mi.get_or_create_channel(client, 'Sub', 'cParent') == 'cSub'
    urls = [c.args[0] for c in client.api.call_args_list]
    assert 'channels/add/' not in urls


def test_get_or_create_channel_created():
    client = make_client(
        api_exc={'channels/get/': request_error(404)},
        api_map={'channels/add/': {'oid': 'cSub'}},
    )
    assert mi.get_or_create_channel(client, 'Sub', 'cParent') == 'cSub'
    _, kwargs = client.api.call_args
    assert kwargs['data'] == {'title': 'Sub', 'parent': 'cParent'}


def test_get_or_create_channel_error():
    # Any error other than a 404 is propagated (the media is counted as an import failure).
    client = make_client(api_exc={'channels/get/': request_error()})
    with pytest.raises(mi.NudgisRequestError):
        mi.get_or_create_channel(client, 'Sub', 'cParent')


def test_get_or_create_channel_unexpected_response():
    client = make_client(api_map={'channels/get/': {}})
    with pytest.raises(RuntimeError):
        mi.get_or_create_channel(client, 'Sub', 'cParent')


def test_ensure_speaker_channel():
    created = []
    client = speaker_channel_client(created)
    cache = {}
    row = {'speaker_email': 'known@x|other@x'}  # only the first speaker is used
    assert mi.ensure_speaker_channel(client, row, ['Courses', '2026'], cache) == 'mscid-c2'
    # Each level is created below the previous one, starting at the personal channel.
    assert created == [('Courses', 'cPerso'), ('2026', 'c1')]
    assert cache == {
        'known@x': 'cPerso', 'known@x/Courses': 'c1', 'known@x/Courses/2026': 'c2',
    }
    # A second media targeting the same channel is resolved from the cache, without any call.
    client.api.reset_mock()
    assert mi.ensure_speaker_channel(client, row, ['Courses', '2026'], cache) == 'mscid-c2'
    client.api.assert_not_called()
    # A sibling channel of the same speaker only creates the missing level.
    assert mi.ensure_speaker_channel(client, row, ['Courses', '2025'], cache) == 'mscid-c3'
    assert created[-1] == ('2025', 'c1')
    assert [c.args[0] for c in client.api.call_args_list] == ['channels/get/', 'channels/add/']


def test_ensure_speaker_channel_personal_channel_only():
    # An empty path targets the personal channel itself (no sub-channel is created).
    created = []
    client = speaker_channel_client(created)
    assert mi.ensure_speaker_channel(client, {'speaker_email': 'known@x'}, [], {}) == 'mscid-cPerso'
    assert not created


def test_ensure_speaker_channel_without_email():
    client = make_client()
    with pytest.raises(RuntimeError):
        mi.ensure_speaker_channel(client, {'speaker_email': ''}, ['Courses'], {})
    with pytest.raises(RuntimeError):
        mi.ensure_speaker_channel(client, None, ['Courses'], {})
    client.api.assert_not_called()


def test_ensure_speaker_channel_unknown_speaker():
    client = make_client(api_map={'users/': {'users': []}})
    with pytest.raises(RuntimeError):
        mi.ensure_speaker_channel(client, {'speaker_email': 'ghost@x'}, ['Courses'], {})


def test_apply_channels_metadata_existing_channel():
    update = mi.ChannelUpdate(
        path=['Migration', 'Course A'],
        description='<p>My description</p>',
        reference='lti:moodle.example.local:245',
    )
    client = make_client(catalog={'channels': [
        {'oid': 'c1', 'title': 'Migration', 'parent_oid': None},
        {'oid': 'c2', 'title': 'Course A', 'parent_oid': 'c1'},
    ]})
    summary = mi.Summary()
    mi.apply_channels_metadata(client, [update], summary)
    called_urls = [call.args[0] for call in client.api.call_args_list]
    assert 'channels/add/' not in called_urls
    _, kwargs = client.api.call_args
    assert kwargs['data'] == {
        'oid': 'c2',
        'description': '<p>My description</p>',
        'external_ref': 'lti:moodle.example.local:245',
    }
    assert update.object_id == 'c2'
    assert summary.channels_updated == 1
    assert summary.channels_failed == 0


def test_apply_channels_metadata_creates_missing_channels():
    update = mi.ChannelUpdate(path=['Migration', 'Course A', 'Year 1'], description='D')
    created: list[dict] = []

    def api(url, **kwargs):
        if url == 'channels/add/':
            created.append(kwargs['data'])
            return {'oid': f'c{len(created) + 1}'}
        return {}

    client = make_client(
        catalog={'channels': [{'oid': 'c1', 'title': 'Migration', 'parent_oid': None}]},
    )
    client.api = mock.MagicMock(side_effect=api)
    summary = mi.Summary()
    mi.apply_channels_metadata(client, [update], summary)
    # Only the missing levels are created, each one below the previous one.
    assert created == [
        {'title': 'Course A', 'parent': 'c1'},
        {'title': 'Year 1', 'parent': 'c2'},
    ]
    edits = [c for c in client.api.call_args_list if c.args[0] == 'channels/edit/']
    assert [c.kwargs['data'] for c in edits] == [{'oid': 'c3', 'description': 'D'}]
    assert summary.channels_updated == 1


def test_apply_channels_metadata_no_channel():
    client = make_client()
    summary = mi.Summary()
    mi.apply_channels_metadata(client, [], summary)
    client.get_catalog.assert_not_called()
    client.api.assert_not_called()
    assert summary.channels_updated == 0


def test_apply_channels_metadata_catalog_error():
    # Without the catalog no channel can be resolved, so all of them are counted as failed.
    client = make_client(catalog_exc=request_error())
    summary = mi.Summary()
    mi.apply_channels_metadata(
        client,
        [mi.ChannelUpdate(path=['A'], description='D'),
         mi.ChannelUpdate(path=['B'], description='D')],
        summary,
    )
    client.api.assert_not_called()
    assert summary.channels_updated == 0
    assert summary.channels_failed == 2


def test_apply_channels_metadata_continues_on_error():
    # A channel that cannot be updated does not prevent the next ones from being updated.
    first = mi.ChannelUpdate(path=['A'], description='D')
    second = mi.ChannelUpdate(path=['B'], description='D')
    client = make_client(catalog={'channels': [
        {'oid': 'c1', 'title': 'A', 'parent_oid': None},
        {'oid': 'c2', 'title': 'B', 'parent_oid': None},
    ]})

    def api(url, **kwargs):
        if url == 'channels/edit/' and kwargs['data']['oid'] == 'c1':
            raise mi.NudgisRequestError('boom', status_code=500)
        return {}

    client.api = mock.MagicMock(side_effect=api)
    summary = mi.Summary()
    mi.apply_channels_metadata(client, [first, second], summary)
    assert first.object_id is None
    assert second.object_id == 'c2'
    assert summary.channels_updated == 1
    assert summary.channels_failed == 1


# -------- planning / report


def test_count_planned():
    groups = {
        'new': group_with('new', metadata={'title': 'T'},
                          annotations=[{'type': 'chapter', 'time': '0'}]),
        'existing': group_with(
            'existing', object_id='vexist',
            annotations=[{'type': 'chapter', 'time': '1000'}],
            existing_elements=['audio:eng', 'subtitle:fre', 'annotation:chapter:1000'],
        ),
    }
    groups['new'].extra_streams[2] = Path('new_2.mp4')
    groups['new'].audio_tracks['eng'] = Path('new_eng.mp3')
    groups['new'].subtitles['fre'] = Path('new_fre.srt')
    groups['existing'].audio_tracks['eng'] = Path('existing_eng.mp3')
    groups['existing'].subtitles['fre'] = Path('existing_fre.srt')
    summary = mi.Summary()
    mi.count_planned(groups, summary)
    assert summary.medias_imported == 1
    assert summary.medias_existing == 1
    assert summary.merged_videos == 1
    assert summary.metadata_applied == 1
    assert summary.audio_tracks_imported == 1
    assert summary.audio_tracks_existing == 1
    assert summary.subtitles_imported == 1
    assert summary.subtitles_existing == 1
    assert summary.annotations_imported == 1
    assert summary.annotations_existing == 1


def test_count_planned_channels():
    # Every valid "channels.csv" entry would be applied by an "--apply" run.
    summary = mi.Summary()
    mi.count_planned({}, summary, channels=[
        mi.ChannelUpdate(path=['A'], description='D'),
        mi.ChannelUpdate(path=['B'], reference='lti:moodle:1'),
    ])
    assert summary.channels_updated == 2


def test_print_summary_smoke():
    summary = mi.Summary(
        medias_imported=2, import_failures=1, channels_updated=1, channels_failed=1,
        invalid_channels=1,
    )
    summary.drop_media('x', mi.UNIMPORTABLE_CORRUPTED)
    # Should not raise in either mode.
    mi.print_summary(summary, applied=True)
    mi.print_summary(summary, applied=False)


# -------- orchestrator


def build_valid_tree(tmp_path):
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm1_eng.mp3')
    write(tmp_path / 'sub' / 'm2.mp4')
    write(tmp_path / 'readme.txt')  # produces a warning
    write(tmp_path / 'annotations' / 'doc.pdf')
    write(
        tmp_path / 'metadata.csv',
        (
            METADATA_HEADER + '\n'
            'm1,"Title 1",slug-1,,,,,2026-01-01T00:00:00,,,,,,,,yes,no,no\n'
            'm2,"Title 2",slug-2,,,,,,,,,,,,,,,\n'
        ).encode('utf-8'),
    )
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title,content,keywords,attachment\n'
        b'm1,chapter,1000,Intro,Hi,,\n'
        b'm1,slide,2000,Slide,,,doc.pdf\n'
        b'm2,chapter,0,Start,,,\n',
    )
    write(
        tmp_path / 'channels.csv',
        (
            CHANNELS_HEADER + '\n'
            'Migration,"<p>Root channel</p>",\n'
            'Migration/sub,,lti:moodle.example.local:245\n'
        ).encode('utf-8'),
    )


@pytest.fixture()
def patched_client():
    client = make_client()
    with mock.patch('examples.mass_import.NudgisClient', return_value=client), \
            mock.patch('examples.mass_import.probe_media', return_value=None):
        yield client


def test_mass_import_missing_source_dir(tmp_path, patched_client):
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path / 'nope'),
        '--channel', 'Migration',
    ]) == 1


@pytest.mark.parametrize('channel, expected', [
    ('Migration', 0),
    ('mscpath-Migration/sub', 0),
    ('c123456789012345678', 0),  # only 19 chars, too short to be an object id
    ('c-1234567890123456789', 0),  # right length but not alphanumeric
    ('mscid-c1234567890123456789', 1),
    ('c1234567890123456789', 1),
    ('cAbCdEfGhIjKlMnOpQrS', 1),
])
def test_mass_import_channel_object_id(tmp_path, patched_client, channel, expected):
    # The main channel must be a title or an "mscpath-...", an object id is rejected.
    build_valid_tree(tmp_path)
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path), '--channel', channel,
    ]) == expected


def test_mass_import_audit_failure(tmp_path, patched_client):
    # A structurally broken metadata.csv (missing mandatory columns) aborts the run.
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'metadata.csv', b'foo,bar\n1,2\n')
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path), '--channel', 'Migration',
    ]) == 1


def test_mass_import_dry_run(tmp_path, patched_client):
    build_valid_tree(tmp_path)
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path), '--channel', 'Migration',
        '--log-level', 'debug',
    ]) == 0
    patched_client.add_media.assert_not_called()
    called_urls = [call.args[0] for call in patched_client.api.call_args_list]
    assert 'channels/edit/' not in called_urls
    assert 'channels/add/' not in called_urls


def test_mass_import_apply(tmp_path, patched_client):
    build_valid_tree(tmp_path)
    mapping_file = tmp_path / 'mapping.csv'
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path), '--channel', 'Migration',
        '--mapping-file', str(mapping_file), '--apply',
    ]) == 0
    assert patched_client.add_media.call_count == 2
    content = mapping_file.read_text()
    assert 'm1,v_migration:m1' in content
    assert 'm2,v_migration:m2' in content
    # The channel metadata are applied once every media has been imported.
    edits = [c for c in patched_client.api.call_args_list if c.args[0] == 'channels/edit/']
    assert [c.kwargs['data'].get('description') for c in edits] == ['<p>Root channel</p>', None]
    assert edits[1].kwargs['data']['external_ref'] == 'lti:moodle.example.local:245'
    add_media_index = min(
        index for index, call in enumerate(patched_client.api.call_args_list)
        if call.args[0] == 'subtitles/add/'
    )
    edit_index = min(
        index for index, call in enumerate(patched_client.api.call_args_list)
        if call.args[0] == 'channels/edit/'
    )
    assert add_media_index < edit_index


def test_mass_import_ids_to_process(tmp_path, patched_client):
    build_valid_tree(tmp_path)
    mapping_file = tmp_path / 'mapping.csv'
    assert mi.mass_import([
        '--conf', 'conf.json', '--source-dir', str(tmp_path), '--channel', 'Migration',
        '--mapping-file', str(mapping_file), '--apply', '--ids-to-process', 'm1',
    ]) == 0
    assert patched_client.add_media.call_count == 1
    content = mapping_file.read_text()
    assert 'm1,v_migration:m1' in content
    assert 'm2' not in content
