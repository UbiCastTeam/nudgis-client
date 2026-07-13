#!/usr/bin/env python3
"""
Combine several video files into a single side-by-side video using ffmpeg.

The input videos are laid out side by side, from left to right and top to bottom.
Up to 6 inputs are supported.

This module is used by ``mass_import.py`` (through :func:`compose_streams`) to combine the
streams of a multi-stream media on the fly, and can also be used as a standalone script to
combine a given list of source files into a destination file::

    ./examples/compose_multistream.py --inputs a.mp4 a_2.mp4 --output combined/a.mp4
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import subprocess
import sys

try:
    from nudgisclient.lib.utils import configure_logging
except ModuleNotFoundError:  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from nudgisclient.lib.utils import configure_logging

logger = logging.getLogger(__name__)

# Maximum number of streams that can be combined into a multi-stream media.
MAX_INPUTS = 6


def _grid_columns(count: int) -> int:
    """Return the number of columns of the side-by-side grid for ``count`` streams."""
    if count <= 4:
        return 2
    return 3


def _xstack_layout(count: int, columns: int) -> str:
    """
    Build the ``xstack`` layout string placing ``count`` inputs on a grid.

    The inputs are laid out from left to right and top to bottom (specifications §3.4).
    """
    parts = []
    for index in range(count):
        column = index % columns
        row = index // columns
        if column == 0:
            x = '0'
        else:
            x = '+'.join(f'w{row * columns + col}' for col in range(column))
        if row == 0:
            y = '0'
        else:
            y = '+'.join(f'h{prev_row * columns}' for prev_row in range(row))
        parts.append(f'{x}_{y}')
    return '|'.join(parts)


def _build_ffmpeg_command(
    inputs: list[Path],
    output: Path,
    audio_indices: list[int],
    ffmpeg: str = 'ffmpeg',
) -> list[str]:
    """
    Build the ffmpeg command combining ``inputs`` into a single ``output`` video.

    ``audio_indices`` lists the inputs that carry an audio stream: their audio is mixed into
    a single track, or mapped as-is when there is only one. The output has no audio when the
    list is empty.
    """
    count = len(inputs)
    columns = _grid_columns(count)
    layout = _xstack_layout(count, columns)
    video_inputs = ''.join(f'[{index}:v]' for index in range(count))
    filter_complex = f'{video_inputs}xstack=inputs={count}:layout={layout}[v]'
    maps = ['-map', '[v]']
    if len(audio_indices) >= 2:
        audio_inputs = ''.join(f'[{index}:a]' for index in audio_indices)
        filter_complex += f';{audio_inputs}amix=inputs={len(audio_indices)}[a]'
        maps += ['-map', '[a]']
    elif len(audio_indices) == 1:
        maps += ['-map', f'{audio_indices[0]}:a']
    command = [ffmpeg, '-y']
    for input_path in inputs:
        command += ['-i', str(input_path)]
    command += ['-filter_complex', filter_complex, *maps, str(output)]
    return command


def _probe_video(path: Path, ffprobe: str = 'ffprobe') -> tuple[int, int, bool]:
    """
    Probe a media file and return its ``(width, height, has_audio)``.

    ``width`` and ``height`` are those of the first video stream (``0`` if there is none).
    """
    try:
        result = subprocess.run(
            [
                ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type,width,height',
                '-of', 'json', str(path),
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as err:
        raise RuntimeError(f'"{ffprobe}" command not found.') from err
    streams = json.loads(result.stdout or '{}').get('streams', [])
    width = height = 0
    has_audio = False
    for stream in streams:
        if stream['codec_type'] == 'video' and not width:
            width, height = stream['width'], stream['height']
        if stream['codec_type'] == 'audio':
            has_audio = True
    return width, height, has_audio


def _build_composition_layout(
    sizes: list[tuple[int, int]],
    area_width: int,
    area_height: int,
) -> dict:
    """
    Describe how the inputs are placed in the combined video (one layer per input).

    ``sizes`` is the ``(width, height)`` of each input, in display order. The positions
    reproduce the side-by-side grid built by ffmpeg's ``xstack`` (see :func:`_xstack_layout`).
    """
    columns = _grid_columns(len(sizes))
    layers = []
    for index, (width, height) in enumerate(sizes):
        column = index % columns
        row = index // columns
        x = sum(sizes[row * columns + col][0] for col in range(column))
        y = sum(sizes[prev_row * columns][1] for prev_row in range(row))
        layer_id = index + 1
        layers.append({
            'id': layer_id,
            'label': f'element-{layer_id}',
            'enabled': True,
            'source': {
                'type': 'video',
                'roi': {'x': x, 'y': y, 'w': width, 'h': height},
                'native_resolution': {'w': area_width, 'h': area_height},
            },
            'x': x,
            'y': y,
            'w': width,
            'h': height,
            'z': layer_id,
        })
    return {
        'composition_area': {'w': area_width, 'h': area_height},
        'composition_data': [{'time': 0, 'layers': layers}],
    }


def compose_streams(
    inputs: list[Path],
    output: Path,
    ffmpeg: str = 'ffmpeg',
    ffprobe: str = 'ffprobe',
) -> None:
    """
    Combine the given ordered video inputs into a single side-by-side ``output`` video.

    The main stream must come first, followed by the secondary streams in display order.
    Inputs without an audio stream are supported: only the inputs that have audio are mixed
    into the resulting track. The parent directory of ``output`` is created if needed.

    A companion JSON file describing the placement of each input (the "layout preset") is
    written next to ``output``, with the same name and a ``.json`` extension.

    Raise ``ValueError`` if the number of inputs is out of range, or ``RuntimeError`` if
    ffmpeg/ffprobe is missing or ffmpeg fails.
    """
    if not 2 <= len(inputs) <= MAX_INPUTS:
        raise ValueError(
            f'Expected between 2 and {MAX_INPUTS} inputs to combine, got {len(inputs)}.'
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    probes = [_probe_video(path, ffprobe=ffprobe) for path in inputs]
    audio_indices = [index for index, (_w, _h, has_audio) in enumerate(probes) if has_audio]
    command = _build_ffmpeg_command(inputs, output, audio_indices, ffmpeg=ffmpeg)
    logger.info('Combining %s inputs into "%s".', len(inputs), output)
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as err:
        raise RuntimeError(f'"{ffmpeg}" command not found.') from err
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr.strip()}')
    logger.info('Combined video written to "%s".', output)

    # Write the layout preset describing the placement of each input next to the output.
    sizes = [(width, height) for width, height, _has_audio in probes]
    area_width, area_height, _ = _probe_video(output, ffprobe=ffprobe)
    layout = _build_composition_layout(sizes, area_width, area_height)
    layout_path = output.with_suffix('.json')
    layout_path.write_text(json.dumps(layout, indent=2))
    logger.info('Composition layout written to "%s".', layout_path)


def compose_multistream(sys_args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        'compose_multistream',
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--inputs',
        help='Ordered list of source video files (main stream first).',
        required=True,
        nargs='+',
        type=Path,
    )
    parser.add_argument(
        '--output',
        help='Path of the combined video file to produce.',
        required=True,
        type=Path,
    )
    parser.add_argument(
        '--ffmpeg',
        help='Path to the ffmpeg executable.',
        default='ffmpeg',
    )
    parser.add_argument(
        '--ffprobe',
        help='Path to the ffprobe executable (used to detect audio streams).',
        default='ffprobe',
    )
    parser.add_argument(
        '--log-level',
        help='Log level.',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug'],
    )
    args = parser.parse_args(sys_args)

    configure_logging(args.log_level.upper())

    for input_path in args.inputs:
        if not input_path.is_file():
            logger.error('Source video "%s" does not exist.', input_path)
            return 1

    try:
        compose_streams(args.inputs, args.output, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    except (ValueError, RuntimeError) as err:
        logger.error('%s', err)
        return 1
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(compose_multistream(sys.argv[1:]))
