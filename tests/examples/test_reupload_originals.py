from pathlib import Path
from unittest import mock

import pytest

import examples.reupload_originals as ro

# -------- helpers


def write(path: Path, content: bytes = b'data') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def resource(path: str, service: str = 'local', res_format: str = 'mp4', file_size: int = 10) -> dict:
    return {
        'manager': {'id': 1, 'service': service, 'code': 'r0123456789abcdef'} if service else None,
        'format': res_format,
        'path': path,
        'file_size': file_size,
    }


def make_client(*, resources_list=None, media=None, config=None, sharing=None, api_exc=None):
    """
    Build a client mock. ``resources_list`` is the list of the successive responses of
    "medias/resources-list/" (the last one is repeated if there are more calls).
    """
    client = ro.NudgisClient()
    client._server_version = (12, 3, 0)
    client.chunked_upload = mock.MagicMock(return_value='upload-id')
    listings = list(resources_list or [[]])

    def api(url, **kwargs):
        if api_exc and url in api_exc:
            raise api_exc[url]
        if url == 'medias/get/':
            return {'info': media if media is not None else {'title': 'Test'}}
        if url == 'medias/resources-list/':
            return {'resources': listings.pop(0) if len(listings) > 1 else listings[0]}
        if url == 'medias/resources-info/':
            return {'deletable': True, 'sharing': sharing or []}
        if url == 'tasks/get-upload-config/':
            return {'config': config if config is not None else {
                'service': 'local', 'code': 'r126d5bee5d38ve1icgkudq8p4ujiz',
            }}
        return {'message': 'ok'}

    client.api = mock.MagicMock(side_effect=api)
    return client


def api_calls(client, url):
    return [call for call in client.api.call_args_list if call.args[0] == url]


# -------- mapping file


def test_read_mapping(tmp_path):
    path = write(tmp_path / 'map.csv', (
        b'source_id,oid\n'
        b'00077418,v126d5681a914tzllpi6\n'
        b'001ed551,v126d5681a91a42ix2mn\n'
    ))
    assert ro.read_mapping(path) == {
        '00077418': 'v126d5681a914tzllpi6',
        '001ed551': 'v126d5681a91a42ix2mn',
    }


def test_read_mapping_extra_columns_and_invalid_lines(tmp_path):
    path = write(tmp_path / 'map.csv', (
        b'source_id,oid,title\n'
        b'a,v1,Some title\n'
        b',v2,No source id\n'
        b'c,,No oid\n'
        b'a,v4,Duplicated source id\n'
    ))
    # Only the first entry is usable, the duplicated source id keeps the first value.
    assert ro.read_mapping(path) == {'a': 'v1'}


def test_read_mapping_duplicated_oid_is_dropped(tmp_path):
    path = write(tmp_path / 'map.csv', b'source_id,oid\na,v1\nb,v1\nc,v2\n')
    # Two files sent to the same media would delete each other, both entries are dropped.
    assert ro.read_mapping(path) == {'c': 'v2'}


def test_read_mapping_missing_column(tmp_path):
    path = write(tmp_path / 'map.csv', b'source,oid\na,v1\n')
    with pytest.raises(ValueError, match='source_id'):
        ro.read_mapping(path)


def test_read_done_source_ids(tmp_path):
    path = write(tmp_path / 'result.csv', (
        b'source_id,oid,status,detail\n'
        b'a,v1,done,1 resource(s) deleted.\n'
        b'b,v2,failed,Some error\n'
        b'c,v3,skipped,Some reason\n'
    ))
    assert ro.read_done_source_ids(path) == {'a'}
    assert ro.read_done_source_ids(tmp_path / 'missing.csv') == set()


# -------- files listing


def test_list_video_files(tmp_path):
    write(tmp_path / 'b.mp4')
    write(tmp_path / 'a.MP4')
    write(tmp_path / 'notes.txt')
    (tmp_path / 'sub').mkdir()
    write(tmp_path / 'sub' / 'c.mp4')

    files = ro.list_video_files(tmp_path)

    # Sorted by name, only the video files of the folder itself.
    assert [path.name for path in files] == ['a.MP4', 'b.mp4']


# -------- resources matching


def test_get_expected_names():
    assert ro.get_expected_names('fa5381aa.mp4') == [
        'fa5381aa.mp4', 'fa5381aa_original.mp4', 'fa5381aa_clean.mp4',
    ]


def test_find_uploaded_resource_prefers_the_new_resource():
    resources = [resource('fa5381aa.mp4'), resource('fa5381aa_original.mp4')]
    # The exact name existed before the upload, so the uploaded file is the new one.
    found = ro.find_uploaded_resource(resources, 'fa5381aa.mp4', {'fa5381aa.mp4'})
    assert found['path'] == 'fa5381aa_original.mp4'


def test_find_uploaded_resource_overwritten_resource():
    resources = [resource('fa5381aa.mp4')]
    # The upload has overwritten a resource that already had the target name.
    found = ro.find_uploaded_resource(resources, 'fa5381aa.mp4', {'fa5381aa.mp4'})
    assert found['path'] == 'fa5381aa.mp4'


def test_find_uploaded_resource_not_found():
    assert ro.find_uploaded_resource([resource('other.mp4')], 'fa5381aa.mp4', {'other.mp4'}) is None


def test_is_managed():
    assert ro.is_managed(resource('a.mp4'))
    assert ro.is_managed(resource('a.mp4', service='object'))
    assert not ro.is_managed(resource('a.mp4', service='youtube'))
    assert not ro.is_managed(resource('', res_format='embed'))
    assert not ro.is_managed({'manager': None, 'format': 'mp4', 'path': 'a.mp4'})


# -------- upload target


def test_get_upload_target():
    client = make_client()
    assert ro.get_upload_target(client, 'v1') == 'r126d5bee5d38ve1icgkudq8p4ujiz'


def test_get_upload_target_without_code():
    client = make_client(config={'service': 'local'})
    with pytest.raises(ValueError, match='usable code'):
        ro.get_upload_target(client, 'v1')


# -------- media processing


def test_process_media_dry_run(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(resources_list=[[resource('old_360.mp4'), resource('media.m3u8', res_format='m3u8')]])
    summary = ro.Summary()

    status, detail = ro.process_media(client, video, 'v1', summary, apply=False)

    assert status == 'planned'
    assert detail == '2 resource(s) to delete.'
    assert summary.uploaded_size == 100
    assert summary.deleted_count == 2
    # Nothing is uploaded, deleted or renamed in dry run mode.
    assert not client.chunked_upload.called
    assert not api_calls(client, 'medias/resources-delete/')
    assert not api_calls(client, 'medias/resources-check/')


def test_process_media_uploads_deletes_and_renames(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(resources_list=[
        # Before the upload.
        [resource('old_360.mp4', file_size=30), resource('old_720.mp4', file_size=70)],
        # After the upload: the portal has added the "_original" particle.
        [
            resource('old_360.mp4', file_size=30),
            resource('old_720.mp4', file_size=70),
            resource('fa5381aa_original.mp4', file_size=100),
        ],
    ])
    summary = ro.Summary()

    status, detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'done'
    assert detail == '2 resource(s) deleted.'
    client.chunked_upload.assert_called_once()
    assert client.chunked_upload.call_args.kwargs['remote_path'] == (
        'r126d5bee5d38ve1icgkudq8p4ujiz/fa5381aa.mp4'
    )
    # The old resources are deleted, the uploaded one is kept.
    deleted = [call.kwargs['data']['names'] for call in api_calls(client, 'medias/resources-delete/')]
    assert deleted == ['old_360.mp4', 'old_720.mp4']
    assert summary.deleted_count == 2
    assert summary.deleted_size == 100
    # The particle added by the portal is removed.
    renames = api_calls(client, 'medias/resources-rename/')
    assert len(renames) == 1
    assert renames[0].kwargs['data'] == {
        'oid': 'v1', 'name': 'fa5381aa_original.mp4', 'new_name': 'fa5381aa.mp4',
    }
    # The resources are refreshed after the upload and after the deletions.
    assert len(api_calls(client, 'medias/resources-check/')) == 2
    assert not summary.warnings


def test_process_media_without_rename(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(resources_list=[
        [resource('old_360.mp4')],
        [resource('old_360.mp4'), resource('fa5381aa.mp4')],
    ])
    summary = ro.Summary()

    status, _detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'done'
    assert not api_calls(client, 'medias/resources-rename/')


def test_process_media_keeps_resources_if_upload_is_not_found(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(resources_list=[[resource('old_360.mp4')]])
    summary = ro.Summary()

    status, detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'failed'
    assert 'has not been added' in detail
    # No resource may be deleted when the uploaded file cannot be found.
    assert not api_calls(client, 'medias/resources-delete/')


def test_process_media_ignores_unmanaged_resources(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(resources_list=[
        [resource('', service='youtube', res_format='youtube')],
        [resource('', service='youtube', res_format='youtube'), resource('fa5381aa.mp4')],
    ])
    summary = ro.Summary()

    status, _detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'done'
    assert not api_calls(client, 'medias/resources-delete/')
    assert any('not hosted by the portal' in warning for warning in summary.warnings)


def test_process_media_skips_shared_resources(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(
        resources_list=[[resource('old_360.mp4')]],
        sharing=[{'oid': 'v1', 'title': 'Test'}, {'oid': 'v2', 'title': 'Other'}],
    )
    summary = ro.Summary()

    status, detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'skipped'
    assert 'v2' in detail
    assert not client.chunked_upload.called
    assert not api_calls(client, 'medias/resources-delete/')


def test_process_media_skips_media_in_recycle_bin(tmp_path):
    video = write(tmp_path / 'fa5381aa.mp4', b'x' * 100)
    client = make_client(media={'title': 'Test', 'trash_data': {'date': '2026-01-01'}})
    summary = ro.Summary()

    status, detail = ro.process_media(client, video, 'v1', summary, apply=True)

    assert status == 'skipped'
    assert 'recycle bin' in detail
    assert not client.chunked_upload.called


def test_delete_resources_ignores_timeouts():
    client = make_client(api_exc={'medias/resources-delete/': ro.NudgisRequestError(
        'HTTP 0 error on "url": read timeout=180'
    )})
    summary = ro.Summary()

    ro.delete_resources(client, 'v1', [resource('old_360.mp4')], summary)

    assert summary.deleted_count == 0
    assert any('timed out' in warning for warning in summary.warnings)


# -------- batch


def test_reupload_files(tmp_path):
    files = [write(tmp_path / 'a.mp4'), write(tmp_path / 'b.mp4'), write(tmp_path / 'unknown.mp4')]
    result_path = tmp_path / 'result.csv'
    client = make_client()
    statuses = {'a': ('done', 'ok'), 'b': ('failed', 'Some error')}

    with mock.patch.object(
        ro, 'process_media', side_effect=lambda ngc, path, oid, summary, apply: statuses[path.stem]
    ):
        summary = ro.reupload_files(
            client, files, {'a': 'v1', 'b': 'v2', 'c': 'v3'}, result_path, set(), apply=True
        )

    assert (summary.done, summary.failed, summary.skipped) == (1, 1, 1)
    # The file without object id and the mapping entry without file are reported.
    assert summary.unmapped == ['unknown']
    assert summary.missing_files == ['c']
    assert result_path.read_text().splitlines() == [
        'source_id,oid,status,detail',
        'a,v1,done,ok',
        'b,v2,failed,Some error',
    ]


def test_reupload_files_resume(tmp_path):
    files = [write(tmp_path / 'a.mp4'), write(tmp_path / 'b.mp4')]
    result_path = write(tmp_path / 'result.csv', b'source_id,oid,status,detail\na,v1,done,ok\n')
    client = make_client()

    with mock.patch.object(ro, 'process_media', return_value=('done', 'ok')) as process:
        summary = ro.reupload_files(
            client, files, {'a': 'v1', 'b': 'v2'}, result_path, {'a'}, apply=True
        )

    assert [call.args[1].name for call in process.call_args_list] == ['b.mp4']
    assert (summary.done, summary.skipped) == (1, 1)
    # The result file is appended, its header is not written again.
    assert result_path.read_text().splitlines() == [
        'source_id,oid,status,detail',
        'a,v1,done,ok',
        'b,v2,done,ok',
    ]


# -------- command line


def test_reupload_originals_dry_run(tmp_path):
    folder = tmp_path / 'videos'
    write(folder / 'fa5381aa.mp4', b'x' * 100)
    write(tmp_path / 'map.csv', b'source_id,oid\nfa5381aa,v1\n')
    result_path = tmp_path / 'result.csv'
    client = make_client(resources_list=[[resource('old_360.mp4')]])
    client.check_server = mock.MagicMock(return_value={})

    with mock.patch.object(ro, 'NudgisClient', return_value=client):
        rc = ro.reupload_originals([
            '--conf', 'conf.json',
            '--folder', str(folder),
            '--csv', str(tmp_path / 'map.csv'),
            '--result-file', str(result_path),
        ])

    assert rc == 0
    assert not client.chunked_upload.called
    assert result_path.read_text().splitlines()[1].startswith('fa5381aa,v1,planned,')


def test_reupload_originals_invalid_folder(tmp_path):
    write(tmp_path / 'map.csv', b'source_id,oid\na,v1\n')
    rc = ro.reupload_originals([
        '--conf', 'conf.json', '--folder', str(tmp_path / 'missing'), '--csv', str(tmp_path / 'map.csv'),
    ])
    assert rc == 1


def test_reupload_originals_empty_folder(tmp_path):
    folder = tmp_path / 'videos'
    folder.mkdir()
    write(tmp_path / 'map.csv', b'source_id,oid\na,v1\n')
    rc = ro.reupload_originals([
        '--conf', 'conf.json', '--folder', str(folder), '--csv', str(tmp_path / 'map.csv'),
    ])
    assert rc == 1


def test_reupload_originals_limit(tmp_path):
    folder = tmp_path / 'videos'
    write(folder / 'a.mp4')
    write(folder / 'b.mp4')
    write(tmp_path / 'map.csv', b'source_id,oid\na,v1\nb,v2\n')
    client = make_client()
    client.check_server = mock.MagicMock(return_value={})

    with (
        mock.patch.object(ro, 'NudgisClient', return_value=client),
        mock.patch.object(ro, 'process_media', return_value=('done', 'ok')) as process,
    ):
        rc = ro.reupload_originals([
            '--conf', 'conf.json',
            '--folder', str(folder),
            '--csv', str(tmp_path / 'map.csv'),
            '--result-file', str(tmp_path / 'result.csv'),
            '--apply',
            '--limit', '1',
        ])

    assert rc == 0
    assert [call.args[1].name for call in process.call_args_list] == ['a.mp4']
