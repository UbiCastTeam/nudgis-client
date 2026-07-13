import json
from pathlib import Path
import subprocess

import pytest

import examples.compose_multistream as cm

# Real sample videos used to exercise ffmpeg for real.
SAMPLES_DIR = Path(__file__).resolve().parent.parent / 'samples'
SAMPLE_VIDEOS = [
    SAMPLES_DIR / 'ball_1920x1080_h264_aac.mp4',
    SAMPLES_DIR / 'mire_1280x720_h264_aac.mp4',
    SAMPLES_DIR / 'ball_720x540_av1_opus.webm',
]
# A video-only sample (no audio stream).
SAMPLE_NO_AUDIO = SAMPLES_DIR / 'ball_640x480_h265.mp4'


def probe_codec_types(path: Path) -> list[str]:
    """Return the codec types (e.g. "video", "audio") of the streams of a media file."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [stream['codec_type'] for stream in json.loads(result.stdout)['streams']]


def _video_size(path: Path) -> tuple[int, int]:
    """Return the ``(width, height)`` of the first video stream of a media file."""
    result = subprocess.run(
        [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'json', str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    stream = json.loads(result.stdout)['streams'][0]
    return stream['width'], stream['height']


# -------- ffmpeg command building


@pytest.mark.parametrize('count, columns', [(2, 2), (4, 2), (5, 3), (6, 3)])
def test_grid_columns(count, columns):
    assert cm._grid_columns(count) == columns


def test_xstack_layout():
    assert cm._xstack_layout(2, 2) == '0_0|w0_0'
    assert cm._xstack_layout(3, 2) == '0_0|w0_0|0_h0'
    assert cm._xstack_layout(5, 3) == '0_0|w0_0|w0+w1_0|0_h0|w3_h0'


def test_build_ffmpeg_command_audio_mapping(tmp_path):
    inputs = [tmp_path / 'a.mp4', tmp_path / 'a_2.mp4']
    output = tmp_path / 'out' / 'a.mp4'
    prefix = ['ffmpeg', '-y', '-i', str(inputs[0]), '-i', str(inputs[1]), '-filter_complex']
    video = '[0:v][1:v]xstack=inputs=2:layout=0_0|w0_0[v]'

    # Both inputs have audio: the tracks are mixed together.
    assert cm._build_ffmpeg_command(inputs, output, [0, 1]) == [
        *prefix, f'{video};[0:a][1:a]amix=inputs=2[a]',
        '-map', '[v]', '-map', '[a]', str(output),
    ]
    # Only one input has audio: it is mapped as-is.
    assert cm._build_ffmpeg_command(inputs, output, [1]) == [
        *prefix, video, '-map', '[v]', '-map', '1:a', str(output),
    ]
    # No input has audio: the output has no audio.
    assert cm._build_ffmpeg_command(inputs, output, []) == [
        *prefix, video, '-map', '[v]', str(output),
    ]


def test_build_composition_layout_two_inputs():
    layout = cm._build_composition_layout([(1920, 1080), (1280, 720)], 3200, 1080)
    assert layout == {
        'composition_area': {'w': 3200, 'h': 1080},
        'composition_data': [{
            'time': 0,
            'layers': [
                {
                    'id': 1, 'label': 'element-1', 'enabled': True,
                    'source': {
                        'type': 'video',
                        'roi': {'x': 0, 'y': 0, 'w': 1920, 'h': 1080},
                        'native_resolution': {'w': 3200, 'h': 1080},
                    },
                    'x': 0, 'y': 0, 'w': 1920, 'h': 1080, 'z': 1,
                },
                {
                    'id': 2, 'label': 'element-2', 'enabled': True,
                    'source': {
                        'type': 'video',
                        'roi': {'x': 1920, 'y': 0, 'w': 1280, 'h': 720},
                        'native_resolution': {'w': 3200, 'h': 1080},
                    },
                    'x': 1920, 'y': 0, 'w': 1280, 'h': 720, 'z': 2,
                },
            ],
        }],
    }


def test_build_composition_layout_grid_positions():
    # 4 inputs -> 2x2 grid: the second row is placed below the first.
    sizes = [(100, 50), (200, 50), (100, 60), (200, 60)]
    layout = cm._build_composition_layout(sizes, 300, 110)
    coords = [(layer['x'], layer['y']) for layer in layout['composition_data'][0]['layers']]
    assert coords == [(0, 0), (100, 0), (0, 50), (100, 50)]


# -------- compose_streams (real ffmpeg)


@pytest.mark.parametrize('count', [2, 3])
def test_compose_streams_creates_video(tmp_path, count):
    output = tmp_path / 'out' / 'combined.mp4'
    cm.compose_streams(SAMPLE_VIDEOS[:count], output)
    assert output.is_file() and output.stat().st_size > 0
    codec_types = probe_codec_types(output)
    assert 'video' in codec_types  # the streams were stacked into a single video stream
    assert 'audio' in codec_types  # the audio tracks were mixed into a single audio stream

    # The layout preset JSON is written next to the output and matches the real video size.
    layout = json.loads(output.with_suffix('.json').read_text())
    layers = layout['composition_data'][0]['layers']
    assert len(layers) == count
    assert layers[0]['source']['roi'] == {'x': 0, 'y': 0, 'w': 1920, 'h': 1080}
    assert [layer['id'] for layer in layers] == list(range(1, count + 1))
    assert (layout['composition_area']['w'], layout['composition_area']['h']) == _video_size(output)


def test_compose_streams_partial_audio(tmp_path):
    # One input has no audio: the audio of the other input is still kept.
    output = tmp_path / 'combined.mp4'
    cm.compose_streams([SAMPLE_NO_AUDIO, SAMPLE_VIDEOS[0]], output)
    codec_types = probe_codec_types(output)
    assert 'video' in codec_types
    assert 'audio' in codec_types


def test_compose_streams_without_audio(tmp_path):
    # No input has audio: the resulting video has no audio stream.
    output = tmp_path / 'combined.mp4'
    cm.compose_streams([SAMPLE_NO_AUDIO, SAMPLE_NO_AUDIO], output)
    assert probe_codec_types(output) == ['video']


def test_compose_streams_invalid_count(tmp_path):
    with pytest.raises(ValueError):
        cm.compose_streams(SAMPLE_VIDEOS[:1], tmp_path / 'o.mp4')
    with pytest.raises(ValueError):
        cm.compose_streams(SAMPLE_VIDEOS * 3, tmp_path / 'o.mp4')  # 9 inputs > maximum


def test_compose_streams_ffmpeg_failure(tmp_path):
    # A non-video input makes ffmpeg exit with a non-zero status.
    bogus = tmp_path / 'bogus.mp4'
    bogus.write_bytes(b'not a video')
    with pytest.raises(RuntimeError, match='ffmpeg failed'):
        cm.compose_streams([bogus, SAMPLE_VIDEOS[0]], tmp_path / 'o.mp4')


def test_compose_streams_ffmpeg_not_found(tmp_path):
    with pytest.raises(RuntimeError, match='not found'):
        cm.compose_streams(SAMPLE_VIDEOS[:2], tmp_path / 'o.mp4', ffmpeg='ffmpeg-does-not-exist')


def test_compose_streams_ffprobe_not_found(tmp_path):
    with pytest.raises(RuntimeError, match='not found'):
        cm.compose_streams(SAMPLE_VIDEOS[:2], tmp_path / 'o.mp4', ffprobe='ffprobe-does-not-exist')


# -------- CLI (real ffmpeg)


def test_cli_success(tmp_path):
    output = tmp_path / 'combined' / 'a.mp4'
    assert cm.compose_multistream([
        '--inputs', str(SAMPLE_VIDEOS[0]), str(SAMPLE_VIDEOS[1]),
        '--output', str(output), '--log-level', 'debug',
    ]) == 0
    assert output.is_file()
    assert 'video' in probe_codec_types(output)


def test_cli_missing_source(tmp_path):
    assert cm.compose_multistream([
        '--inputs', str(SAMPLE_VIDEOS[0]), str(tmp_path / 'missing.mp4'),
        '--output', str(tmp_path / 'o.mp4'),
    ]) == 1


def test_cli_compose_error(tmp_path):
    bogus = tmp_path / 'bogus.mp4'
    bogus.write_bytes(b'not a video')
    assert cm.compose_multistream([
        '--inputs', str(bogus), str(SAMPLE_VIDEOS[0]), '--output', str(tmp_path / 'o.mp4'),
    ]) == 1
