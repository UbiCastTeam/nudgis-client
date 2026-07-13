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
        side_effect=lambda **kwargs: {'oid': 'v_' + kwargs.get('external_ref', 'x')}
    )

    def api(url, **kwargs):
        if api_exc and url in api_exc:
            raise api_exc[url]
        return (api_map or {}).get(url, {})

    client.api = mock.MagicMock(side_effect=api)
    return client


METADATA_HEADER = (
    'source_id,title,slug,description,keywords,categories,language,creation,'
    'speaker_name,speaker_email,company_name,company_url,license_name,license_url,'
    'channel,validated,unlisted,detect_slides'
)


# -------- scan


def test_scan_basic(tmp_path):
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm1_eng.mp3')
    write(tmp_path / 'sub' / 'm2.mp4')
    write(tmp_path / 'metadata.csv', b'ignored')
    write(tmp_path / 'annotations' / 'annotations.csv', b'ignored')
    write(tmp_path / 'readme.txt')  # unexpected file -> warning

    report = mi.Report()
    groups = mi.scan_source_tree(tmp_path, None, report)

    assert set(groups) == {'m1', 'm2'}
    assert set(groups['m1'].subtitles) == {'fre'}
    assert set(groups['m1'].audio_tracks) == {'eng'}
    assert groups['m2'].rel_dir == Path('sub')
    assert not report.errors
    assert any('Ignoring unexpected file' in w for w in report.warnings)


def test_scan_filename_too_long(tmp_path):
    # 251 chars: allowed by the filesystem but above MAX_FIELD_LENGTH (250).
    write(tmp_path / ('x' * 247 + '.mp4'))
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert any('File name too long' in err for err in report.errors)


def test_scan_multistream_warnings(tmp_path):
    write(tmp_path / 'multi.mp4')
    write(tmp_path / 'multi_stream2.mp4')
    write(tmp_path / 'multi_stream4.mp4')  # gap -> non-contiguous warning
    write(tmp_path / 'noLang.srt')  # no "_lang" suffix

    report = mi.Report()
    groups = mi.scan_source_tree(tmp_path, None, report)

    assert set(groups['multi'].extra_streams) == {2, 4}
    assert any('non-contiguous' in w for w in report.warnings)
    assert any('will be combined with ffmpeg' in w for w in report.warnings)
    assert any('naming rule' in err for err in report.warnings)


def test_scan_duplicate_source_id(tmp_path):
    write(tmp_path / 'a' / 'dup.mp4')
    write(tmp_path / 'b' / 'dup.mp4')
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert any('Duplicate source id' in err for err in report.errors)


def test_scan_multistream_without_main(tmp_path):
    write(tmp_path / 'clip.mp4')
    write(tmp_path / 'clip_stream2.mp4')
    write(tmp_path / 'clip_stream2_stream2.mp4')  # secondary of "clip_2", which is not a main media
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert any('has no main media' in err for err in report.errors)


def test_scan_multistream_out_of_range(tmp_path):
    write(tmp_path / 'range.mp4')
    write(tmp_path / 'range_stream1.mp4')  # index < 2
    write(tmp_path / 'range_stream7.mp4')  # index > 6
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert sum('out-of-range' in err for err in report.errors) == 2


def test_scan_linked_orphan(tmp_path):
    write(tmp_path / 'vid.mp4')
    write(tmp_path / 'orphan_fre.srt')  # base "orphan" has no media
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert any('has no main media' in err for err in report.errors)


def test_scan_linked_duplicate(tmp_path):
    write(tmp_path / 'vid.mp4')
    write(tmp_path / 'vid_fre.srt')
    write(tmp_path / 'vid_fre.vtt')  # same base + language
    report = mi.Report()
    mi.scan_source_tree(tmp_path, None, report)
    assert any('Duplicate subtitle file' in err for err in report.errors)


def test_scan_empty(tmp_path):
    report = mi.Report()
    groups = mi.scan_source_tree(tmp_path, None, report)
    assert groups == {}
    assert any('No media file to import found' in err for err in report.errors)


def test_scan_ids_to_process(tmp_path):
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm2.mp4')
    write(tmp_path / 'm2_fre.srt')
    write(tmp_path / 'm2_stream2.mp4')
    report = mi.Report()
    groups = mi.scan_source_tree(tmp_path, ['m1'], report)
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
    groups = mi.scan_source_tree(tmp_path, ['m1'], report)
    assert set(groups) == {'m1'}
    assert not groups['m1'].extra_streams
    assert not report.errors


# -------- metadata


def test_metadata_missing_file(tmp_path):
    report = mi.Report()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(tmp_path / 'metadata.csv', groups, None, report)
    assert groups['m1'].metadata is None
    assert any('No "metadata.csv"' in w for w in report.warnings)


def test_metadata_missing_columns(tmp_path):
    path = write(tmp_path / 'metadata.csv', b'foo,bar\n1,2\n')
    report = mi.Report()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report)
    assert groups['m1'].metadata is None
    assert any('missing mandatory columns' in err for err in report.errors)


def test_metadata_unknown_column(tmp_path):
    path = write(tmp_path / 'metadata.csv', b'source_id,title,weird\nm1,Title,x\n')
    report = mi.Report()
    mi.validate_metadata_csv(path, {'m1': group('m1')}, None, report)
    assert any('unknown column "weird"' in w for w in report.warnings)


def test_metadata_valid(tmp_path):
    content = (
        METADATA_HEADER + '\n'
        'm1,"Title 1",my-slug,desc,a|b,c1|c2,fre,2026-02-26T17:10:00,'
        'Jane|John,jane@x|john@x,Comp,http://c,Lic,http://l,,yes,no,no\n'
    )
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    report = mi.Report()
    groups = {'m1': group('m1')}
    mi.validate_metadata_csv(path, groups, None, report)
    assert groups['m1'].metadata is not None
    assert not report.errors


def test_metadata_all_errors(tmp_path):
    content = (
        'source_id,title,slug,language,creation,speaker_name,speaker_email,validated\n'
        ',Title,,,,,,\n'  # empty source_id
        'good,Title,goodslug,fre,2026-01-01T00:00:00,,,yes\n'  # valid row (slug recorded)
        'good,Title,other,fre,,,,\n'  # duplicate source_id
        'ghost,Title,,,,,,\n'  # source_id not in groups
        'm2,,Bad Slug,xx,not-a-date,A|B,a@x,maybe\n'  # title/slug/lang/date/speaker/validated
        'm3,Title,goodslug,,,,,\n'  # duplicate slug
    )
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    groups = {'good': group('good'), 'm2': group('m2'), 'm3': group('m3')}
    report = mi.Report()
    mi.validate_metadata_csv(path, groups, None, report)
    joined = '\n'.join(report.errors)
    assert 'empty "source_id"' in joined
    assert 'duplicate "source_id"' in joined
    assert '"source_id" "ghost"' in joined
    assert 'empty "title"' in joined
    assert 'invalid "slug" "bad slug"' in joined
    assert 'is already used' in joined
    assert 'invalid "language" "xx"' in joined
    assert 'invalid "creation"' in joined
    assert 'different number of values' in joined
    assert 'invalid "validated" "maybe"' in joined


def test_metadata_too_long_field(tmp_path):
    long_title = 'T' * 300
    path = write(
        tmp_path / 'metadata.csv',
        (METADATA_HEADER + f'\nm1,{long_title},,,,,,,,,,,,,,,,\n').encode('utf-8'),
    )
    report = mi.Report()
    mi.validate_metadata_csv(path, {'m1': group('m1')}, None, report)
    assert any('value too long' in err for err in report.errors)


def test_metadata_media_without_row(tmp_path):
    path = write(
        tmp_path / 'metadata.csv', (METADATA_HEADER + '\nm1,Title,,,,,,,,,,,,,,,,\n').encode()
    )
    report = mi.Report()
    mi.validate_metadata_csv(path, {'m1': group('m1'), 'm2': group('m2')}, None, report)
    assert any('Media "m2" has no row' in w for w in report.warnings)


def test_metadata_csv_ids_to_process(tmp_path):
    content = METADATA_HEADER + '\nm1,Title 1,,,,,,,,,,,,,,,,\nm2,Title 2,,,,,,,,,,,,,,,,\n'
    path = write(tmp_path / 'metadata.csv', content.encode('utf-8'))
    groups = {'m1': group('m1')}
    report = mi.Report()
    mi.validate_metadata_csv(path, groups, ['m1'], report)
    assert groups['m1'].metadata is not None
    assert not report.errors


# -------- annotations


def test_annotations_no_dir(tmp_path):
    report = mi.Report()
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report)
    assert groups['m1'].annotations == []
    assert not report.errors


def test_annotations_dir_without_csv(tmp_path):
    (tmp_path / 'annotations').mkdir()
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report)
    assert any('No "annotations.csv"' in err for err in report.errors)


def test_annotations_missing_columns(tmp_path):
    write(tmp_path / 'annotations' / 'annotations.csv', b'source_id,time\nm1,1\n')
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report)
    assert any('missing mandatory columns' in err for err in report.errors)


def test_annotations_unknown_column(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,weird\nm1,slide,1,x\n',
    )
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report)
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
    groups = {'m1': group('m1')}
    mi.validate_annotations_csv(tmp_path, groups, None, report)
    assert len(groups['m1'].annotations) == 2
    assert not report.errors


def test_annotations_csv_ids_to_process(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title\nm1,chapter,1000,Intro\nm2,chapter,2000,Other\n',
    )
    groups = {'m1': group('m1')}
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, groups, ['m1'], report)
    assert len(groups['m1'].annotations) == 1
    assert not report.errors


def test_annotations_all_errors(tmp_path):
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        b'source_id,type,time,title,attachment\n'
        b',chapter,1,,\n'  # empty source_id
        b'ghost,slide,1,Title,a.pdf\n'  # source_id not in groups
        b'm1,,1,Title,\n'  # empty type
        b'm1,chapter,1,,\n'  # chapter without title
        b'm1,chapter,1,Title,doc.pdf\n'  # chapter with attachment
        b'm1,slide,abc,Title,\n'  # slide without attachment + non-integer time
        b'm1,slide,1,Title,missing.pdf\n',  # attachment does not exist
    )
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report)
    joined = '\n'.join(report.errors)
    assert 'empty "source_id"' in joined
    assert '"source_id" "ghost"' in joined
    assert '"type" cannot be empty' in joined
    assert 'requires a\n"title"' in joined or 'requires a "title"' in joined
    assert 'attachment cannot be linked to "chapter"' in joined
    assert 'attachment is required for "slide"' in joined
    assert 'invalid "time" "abc"' in joined
    assert 'attachment "missing.pdf"' in joined


def test_annotations_too_long_field(tmp_path):
    long_title = 'T' * 300
    write(
        tmp_path / 'annotations' / 'annotations.csv',
        (f'source_id,type,time,title\nm1,chapter,1000,{long_title}\n').encode('utf-8'),
    )
    report = mi.Report()
    mi.validate_annotations_csv(tmp_path, {'m1': group('m1')}, None, report)
    assert any('value too long' in err for err in report.errors)


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


def test_validate_media_integrity(tmp_path):
    g = group('m1')
    g.extra_streams[2] = Path('m1_2.mp4')
    g.audio_tracks['eng'] = Path('m1_eng.mp3')
    report = mi.Report()
    with mock.patch.object(mi, 'probe_media', return_value='bad'):
        mi.validate_media_integrity({'m1': g}, report)
    assert len(report.errors) == 3

    report = mi.Report()
    with mock.patch.object(mi, 'probe_media', return_value=None):
        mi.validate_media_integrity({'m1': g}, report)
    assert not report.errors


# -------- server checks


def request_error(status_code=500):
    return mi.NudgisRequestError('boom', status_code=status_code)


def test_server_unreachable():
    report = mi.Report()
    client = make_client(check_server_exc=RuntimeError('down'))
    mi.server_checks(client, {}, report)
    assert any('Cannot reach' in err for err in report.errors)
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
            'users/': {'users': [{'email': 'known@x'}]},
        },
    )
    report = mi.Report()
    mi.server_checks(client, groups, report)
    errors = '\n'.join(report.errors)
    warnings = '\n'.join(report.warnings)
    # An already used slug is only a warning (the media may be re-imported).
    assert 'Slug "taken"' in warnings
    assert 'Slug "free"' not in warnings
    assert 'Annotation type "custom"' in errors
    assert 'targets "mscspeaker" but has no' in errors
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
    mi.server_checks(client, {'a': g}, report)
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
    mi.server_checks(client, {'a': g}, report)
    assert g.existing_elements == []
    assert not report.errors


def test_server_existing_elements_error():
    g = group_with('a', object_id='vexist')
    err = request_error(status_code=500)
    client = make_client(api_exc={
        'medias/audio/tracks/list/': err,
        'subtitles/': err,
        'annotations/list/': err,
    })
    report = mi.Report()
    mi.server_checks(client, {'a': g}, report)
    assert any('Failed to get audio tracks' in e for e in report.errors)
    assert any('Failed to get subtitles' in e for e in report.errors)
    assert any('Failed to get annotations' in e for e in report.errors)


def test_server_catalog_error():
    client = make_client(catalog_exc=request_error())
    report = mi.Report()
    mi.server_checks(client, {'a': group_with('a', metadata={'slug': 's'})}, report)
    assert any('check slugs' in w for w in report.warnings)


def test_server_annotation_types_error():
    client = make_client(api_exc={'annotations/types/list/': request_error()})
    report = mi.Report()
    mi.server_checks(
        client, {'a': group_with('a', annotations=[{'type': 'custom'}])}, report
    )
    assert any('annotation types' in w for w in report.warnings)


def test_server_speaker_user_missing():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_map={'users/': {'users': []}})
    report = mi.Report()
    mi.server_checks(client, groups, report)
    assert any('does not match any Nudgis user' in err for err in report.errors)


def test_server_speaker_user_error():
    groups = {'b': group_with('b', metadata={'channel': 'mscspeaker',
                                             'speaker_email': 'ghost@x'})}
    client = make_client(api_exc={'users/': request_error()})
    report = mi.Report()
    mi.server_checks(client, groups, report)
    assert any('Could not check speaker' in w for w in report.warnings)


def test_server_channel_missing():
    groups = {'d': group_with('d', metadata={'channel': 'mscid-CID'})}
    client = make_client(api_exc={'channels/get/': request_error()})
    report = mi.Report()
    mi.server_checks(client, groups, report)
    assert any('Failed to get channel' in err for err in report.errors)


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
        'unlisted': 'no', 'detect_slides': 'no', 'keywords': 'a,b',
        'category': 'c1\nc2', 'speaker': 'N1|N2', 'speaker_email': 'e1|e2',
    }


def test_build_media_metadata_no_row():
    assert mi.build_media_metadata(None) == {}


@pytest.mark.parametrize('main, rel_dir, row, expected', [
    ('Migration', '.', {'channel': 'mscid-x'}, 'mscid-x'),
    ('Migration', 'sub/deep', {'channel': ''}, 'mscpath-Migration/sub/deep'),
    ('mscpath-Root', 'a', None, 'mscpath-Root/a'),
    ('Migration', '.', None, 'mscpath-Migration'),
])
def test_resolve_channel(main, rel_dir, row, expected):
    assert mi.resolve_channel(main, Path(rel_dir), row) == expected


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

    mapping = mi.import_media(
        client, groups, 'Migration', tmp_path, mapping_file, tmp_path / 'temp', ['m1']
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

    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, mapping_file, tmp_path / 'temp'
    )

    assert mapping == {'m1': 'vexist'}
    client.add_media.assert_not_called()
    called_urls = [call.args[0] for call in client.api.call_args_list]
    assert 'subtitles/add/' not in called_urls
    assert 'medias/audio/tracks/add/' not in called_urls
    assert 'annotations/post/' not in called_urls


def test_import_media_without_metadata_row(tmp_path):
    write(tmp_path / 'm1.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('sub'))
    client = make_client()
    mapping = mi.import_media(
        client, {'m1': g1}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp'
    )
    assert mapping == {'m1': 'v_migration:m1'}
    _, kwargs = client.add_media.call_args
    assert kwargs['title'] == 'm1'
    assert kwargs['channel'] == 'mscpath-Migration/sub'


def test_import_media_continues_on_upload_error(tmp_path):
    # A media whose upload fails is reported but does not stop the following ones.
    write(tmp_path / 'm1.mp4')
    write(tmp_path / 'm2.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g2 = mi.MediaGroup('m2', None, tmp_path / 'm2.mp4', Path('.'))
    mapping_file = tmp_path / 'mapping.csv'

    client = make_client()

    def add_media(**kwargs):
        if kwargs['external_ref'] == 'migration:m1':
            raise mi.NudgisRequestError('upload failed', status_code=500)
        return {'oid': 'v_' + kwargs['external_ref']}

    client.add_media = mock.MagicMock(side_effect=add_media)

    mapping = mi.import_media(
        client, {'m1': g1, 'm2': g2}, 'Migration', tmp_path, mapping_file, tmp_path / 'temp'
    )

    # The failed media is excluded from the mapping; the next one is still imported.
    assert mapping == {'m2': 'v_migration:m2'}
    assert client.add_media.call_count == 2
    assert mapping_file.read_text() == 'source_id,oid\nm2,v_migration:m2\n'


def test_import_media_continues_on_linked_element_error(tmp_path):
    # An error while attaching a linked file is reported but does not stop processing.
    write(tmp_path / 'm1.mp4')
    sub_path = write(tmp_path / 'm1_fre.srt')
    write(tmp_path / 'm2.mp4')
    g1 = mi.MediaGroup('m1', None, tmp_path / 'm1.mp4', Path('.'))
    g1.subtitles['fre'] = sub_path
    g2 = mi.MediaGroup('m2', None, tmp_path / 'm2.mp4', Path('.'))

    client = make_client()

    def api(url, **kwargs):
        if url == 'subtitles/add/':
            raise mi.NudgisRequestError('subtitle failed', status_code=500)
        return {}

    client.api = mock.MagicMock(side_effect=api)

    mapping = mi.import_media(
        client, {'m1': g1, 'm2': g2}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp'
    )

    # m1 failed while attaching its subtitle, so it is not recorded as imported; m2 is.
    assert set(mapping) == {'m2'}
    assert client.add_media.call_count == 2


def test_import_media_multistream(tmp_path):
    # A multi-stream media is combined into a temporary file, uploaded, then cleaned up.
    main = write(tmp_path / 'multi.mp4')
    second = write(tmp_path / 'multi_2.mp4')
    temp_dir = tmp_path / 'temp'
    g = mi.MediaGroup('multi', None, main, Path('.'))
    g.extra_streams[2] = second

    client = make_client()
    captured = {}

    def fake_compose(inputs, output, ffmpeg='ffmpeg', ffprobe='ffprobe'):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'composed')
        # compose_streams writes the layout preset next to the output (see compose_multistream).
        output.with_suffix('.json').write_text('{"composition_area": {"w": 1280, "h": 640}}')
        captured['call'] = (inputs, output, ffmpeg, ffprobe)

    with mock.patch.object(mi, 'compose_streams', side_effect=fake_compose):
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv', temp_dir,
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


def test_import_media_multistream_skipped_when_exists(tmp_path):
    # An already imported multi-stream media is neither combined nor re-uploaded.
    g = mi.MediaGroup('multi', 'vexist', tmp_path / 'multi.mp4', Path('.'))
    g.extra_streams[2] = tmp_path / 'multi_2.mp4'
    client = make_client()
    with mock.patch.object(mi, 'compose_streams') as compose:
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp'
        )
    compose.assert_not_called()
    client.add_media.assert_not_called()
    assert mapping == {'multi': 'vexist'}


def test_import_media_multistream_compose_error(tmp_path):
    # A composition failure is reported and does not stop the process.
    main = write(tmp_path / 'multi.mp4')
    g = mi.MediaGroup('multi', None, main, Path('.'))
    g.extra_streams[2] = write(tmp_path / 'multi_2.mp4')
    client = make_client()
    with mock.patch.object(mi, 'compose_streams', side_effect=RuntimeError('ffmpeg failed')):
        mapping = mi.import_media(
            client, {'multi': g}, 'Migration', tmp_path, tmp_path / 'map.csv', tmp_path / 'temp'
        )
    assert mapping == {}
    client.add_media.assert_not_called()


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


def test_mass_import_audit_failure(tmp_path, patched_client):
    write(tmp_path / 'metadata.csv', (METADATA_HEADER + '\nm1,Title,,,,,,,,,,,,,,,,\n').encode())
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
