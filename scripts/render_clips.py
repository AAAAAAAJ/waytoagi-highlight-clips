#!/usr/bin/env python3
"""Render a validated highlight project. Requires Pillow, ffmpeg and ffprobe."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow is required. Install it in your Python environment: python3 -m pip install Pillow")

SKILL = Path(__file__).resolve().parents[1]
SIZE = (1080, 1920)
VIDEO_BOX = (40, 550, 1000, 810)
THREADS = min(2, os.cpu_count() or 1)
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")


class ProjectError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProjectError(message)


def number(value, label, minimum=None):
    require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value), f"{label}: expected a finite number")
    value = float(value)
    require(minimum is None or value >= minimum, f"{label}: must be >= {minimum}")
    return value


def nonempty(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label}: expected a nonempty string")
    require(not any(ord(ch) < 32 and ch not in '\n\t' for ch in value), f"{label}: contains control characters")
    return value.strip()


def resolved(base, value, label):
    text = nonempty(value, label)
    p = Path(text).expanduser()
    return p.resolve() if p.is_absolute() else (base / p).resolve()


def probe(source, ffprobe):
    run = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)], capture_output=True, text=True, check=False)
    require(run.returncode == 0, f"ffprobe failed: {run.stderr[-1500:]}")
    data = json.loads(run.stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    require(videos and audios, 'source must contain video and audio streams')
    v = videos[0]
    rotation = [s.get('rotation', 0) for s in v.get('side_data_list', [])]
    require(all(abs(float(r)) % 360 < .01 for r in rotation), 'source has rotation metadata; normalize its orientation before defining crop coordinates')
    duration = float(data['format']['duration'])
    require(math.isfinite(duration) and duration > 0, 'source duration is unavailable')
    return {'width': v['width'], 'height': v['height'], 'duration': duration, 'video_index': v['index'], 'audio_index': audios[0]['index']}


def font_path(style, base):
    if style.get('font'):
        p = resolved(base, style['font'], 'style.font')
    else:
        candidates = [
            '/System/Library/Fonts/Hiragino Sans GB.ttc', '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/STHeiti Medium.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', 'C:/Windows/Fonts/msyh.ttc',
        ]
        p = next((Path(s) for s in candidates if Path(s).is_file()), None)
        require(p is not None, 'No supported Chinese font found; set style.font to a local TTF/OTF/TTC file')
    require(p.is_file(), f'Font not found: {p}')
    bold_index = style.get('font_bold_index', 2 if 'hiragino sans gb' in p.name.lower() else 0)
    require(isinstance(bold_index, int) and not isinstance(bold_index, bool) and bold_index >= 0, 'style.font_bold_index must be a nonnegative integer')
    try:
        regular = ImageFont.truetype(str(p), 52, index=0)
        ImageFont.truetype(str(p), 72, index=bold_index)
    except (OSError, ValueError) as error:
        raise ProjectError(f'Cannot load font/index: {p}: {error}') from error
    return p.resolve(), bold_index, regular.getname()[0]


def crop(value, meta, label):
    require(isinstance(value, list) and len(value) == 4, f'{label}: expected [x,y,width,height]')
    require(all(isinstance(x, int) and not isinstance(x, bool) for x in value), f'{label}: all coordinates must be integers')
    x, y, w, h = value
    require(x >= 0 and y >= 0 and w > 0 and h > 0, f'{label}: invalid dimensions')
    require(x + w <= meta['width'] and y + h <= meta['height'], f'{label}: outside source frame {meta["width"]}x{meta["height"]}')
    require(all(v % 2 == 0 for v in value), f'{label}: use even crop coordinates and dimensions for YUV420')
    return value


def missing_intervals(start, end, keeps):
    cursor, missing = start, []
    # Coverage is a source-space union; edit playback order can differ or repeat.
    for a, b in sorted(keeps):
        if b <= cursor or a >= end:
            continue
        if a > cursor + .00001:
            missing.append([cursor, min(a, end)])
        cursor = max(cursor, min(b, end))
        if cursor >= end:
            break
    if cursor < end - .00001:
        missing.append([cursor, end])
    return missing


def map_captions(clip, fps):
    mapped = []
    timebase = clip.get('caption_timebase', 'window')
    require(timebase in ('window', 'output'), f'{clip["id"]}: caption_timebase must be window or output')
    captions = clip.get('captions', [])
    require(isinstance(captions, list), f'{clip["id"]}: captions must be an array')
    for i, item in enumerate(captions):
        label = f'clip {clip["id"]} caption[{i}]'
        require(isinstance(item, dict), f'{label}: expected an object')
        s, e = number(item.get('start'), label + '.start', 0), number(item.get('end'), label + '.end', 0)
        text = nonempty(item.get('text'), label + '.text')
        require(e > s, f'{label}: end must be greater than start')
        require(isinstance(item.get('editorial', False), bool), f'{label}.editorial must be boolean')
        if timebase == 'window':
            gaps = missing_intervals(s, e, clip['keep'])
            require(not gaps, f'{label} {text!r} spans removed window intervals {gaps}. Split/rewrite the caption against the retained speech, or explicitly supply caption_timebase="output" with reviewed output timings. No caption was dropped.')
            groups, cursor = [], 0.0
            for a, b in clip['keep']:
                left, right = max(s, a), min(e, b)
                if right > left:
                    piece = {'source_start': left, 'source_end': right,
                             'start': cursor + left - a, 'end': cursor + right - a}
                    if groups and abs(groups[-1]['end'] - piece['start']) < .00001 and abs(groups[-1]['source_end'] - left) < .00001:
                        groups[-1]['end'] = piece['end']
                        groups[-1]['source_end'] = right
                    else:
                        groups.append(piece)
                cursor += b - a
            require(groups, f'{label}: lies entirely outside keep intervals')
            for group in groups:
                require(abs(group['source_start'] - s) < .00001 and abs(group['source_end'] - e) < .00001,
                        f'{label} {text!r} is only partially reused at output [{group["start"]},{group["end"]}] '
                        f'(source [{group["source_start"]},{group["source_end"]}] of [{s},{e}]). '
                        'Split the caption at the edit boundaries and verify the retained words, or provide reviewed output captions. '
                        'Another full occurrence cannot fill missing words in this occurrence.')
            intervals = [(group['start'], group['end']) for group in groups]
        else:
            intervals = [(s, e)]
        for out_start, out_end in intervals:
            require(out_end <= clip['duration'] + .00001, f'{label}: end {out_end} exceeds output duration {clip["duration"]}')
            mapped.append({'start': round(out_start, 6), 'end': round(out_end, 6), 'text': text, **({'editorial': True} if item.get('editorial') else {})})
    mapped.sort(key=lambda x: (x['start'], x['end']))
    for left, right in zip(mapped, mapped[1:]):
        require(right['start'] >= left['end'] - .00001, f'clip {clip["id"]}: overlapping captions {left["text"]!r} / {right["text"]!r}; revise timings')
    return mapped


def load_project(path, ffprobe):
    require(path.is_file(), f'Project JSON not found: {path}')
    try:
        project = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ProjectError(f'Cannot read project JSON: {error}') from error
    require(isinstance(project, dict) and project.get('schema_version') == 1, 'schema_version must be 1')
    base = path.parent
    project = copy.deepcopy(project)
    project['source'] = str(resolved(base, project.get('source'), 'source'))
    require(Path(project['source']).is_file(), f'Source not found: {project["source"]}')
    for key in ('output_dir', 'work_dir'):
        project[key] = str(resolved(base, project.get(key), key))
        require(Path(project[key]) != base, f'{key} must be a dedicated directory, not the project directory itself')
    output, work = Path(project['output_dir']), Path(project['work_dir'])
    require(output != work and output not in work.parents and work not in output.parents, 'output_dir and work_dir must be separate non-nested directories')
    fps = project.get('fps', 25)
    require(isinstance(fps, int) and not isinstance(fps, bool) and 1 <= fps <= 60, 'fps must be an integer from 1 to 60')
    project['fps'] = fps
    event = project.get('event')
    require(isinstance(event, dict), 'event must contain name, date, footer')
    for key in ('name', 'date', 'footer'):
        event[key] = nonempty(event.get(key), 'event.' + key)
    style = project.setdefault('style', {})
    require(isinstance(style, dict), 'style must be an object')
    require(style.get('preset', 'waytoagi-light') == 'waytoagi-light', 'Only style.preset="waytoagi-light" is supported')
    style['preset'] = 'waytoagi-light'
    logo = resolved(base, style['logo'], 'style.logo') if style.get('logo') else SKILL / 'assets' / 'brand-logo-white.png'
    require(logo.is_file(), f'Logo not found: {logo}')
    try:
        with Image.open(logo) as image:
            image.verify()
    except (OSError, ValueError) as error:
        raise ProjectError(f'Invalid logo image: {error}') from error
    style['logo'] = str(logo.resolve())
    font, index, family = font_path(style, base)
    style.update(font=str(font), font_bold_index=index, font_family=family)
    meta = probe(Path(project['source']), ffprobe)
    project['source_metadata'] = meta
    clips = project.get('clips')
    require(isinstance(clips, list) and clips, 'clips must be a nonempty array')
    ids = set()
    for c in clips:
        require(isinstance(c, dict), 'Every clip must be an object')
        key = c.get('id')
        require(isinstance(key, str) and bool(ID_PATTERN.fullmatch(key)), f'Unsafe clip id {key!r}; use 1–48 ASCII letters, digits, underscores or hyphens, starting with a letter/digit')
        require(key not in ids, f'Duplicate clip id: {key}')
        ids.add(key)
        for field in ('name', 'tag', 'context', 'speaker', 'kind', 'takeaway'):
            c[field] = nonempty(c.get(field), f'clip {key}.{field}')
        c['hook'] = str(c.get('hook', ''))
        require(isinstance(c.get('headline'), list) and len(c['headline']) == 2, f'clip {key}.headline must contain exactly two strings')
        c['headline'] = [nonempty(t, f'clip {key}.headline') for t in c['headline']]
        start = number(c.get('source_start'), f'clip {key}.source_start', 0)
        end = number(c.get('source_end'), f'clip {key}.source_end', 0)
        require(start < end <= meta['duration'] + .00001, f'clip {key}: invalid source window [{start},{end}]')
        require(isinstance(c.get('keep'), list) and c['keep'], f'clip {key}.keep must be nonempty')
        keeps = []
        for j, interval in enumerate(c['keep']):
            require(isinstance(interval, list) and len(interval) == 2, f'clip {key}.keep[{j}] must be [start,end]')
            a, b = [number(t, f'clip {key}.keep[{j}]', 0) for t in interval]
            require(a < b <= end - start + .00001, f'clip {key}.keep[{j}]: each range must be inside the source window')
            require(all(abs(v * fps - round(v * fps)) < .00001 for v in (a, b)), f'clip {key}.keep[{j}]: endpoints must be on the 1/{fps}-second frame grid; round deliberately before rendering')
            keeps.append([a, b])
        c['keep'] = keeps
        c['duration'] = round(sum(b - a for a, b in keeps), 6)
        default_crop = [0, 0, meta['width'] - meta['width'] % 2, meta['height'] - meta['height'] % 2]
        c['crop'] = crop(c.get('crop', default_crop), meta, f'clip {key}.crop')
        segment_crops = c.get('segment_crops', [c['crop']] * len(keeps))
        require(isinstance(segment_crops, list) and len(segment_crops) == len(keeps), f'clip {key}.segment_crops must have one entry per keep')
        c['segment_crops'] = [crop(v or c['crop'], meta, f'clip {key}.segment_crops[{j}]') for j, v in enumerate(segment_crops)]
        c['captions'] = map_captions(c, fps)
        c['caption_timebase'] = 'output'
        require(isinstance(c.get('broll', []), list), f'clip {key}.broll must be an array')
        previous_end = 0.0
        for j, br in enumerate(c.setdefault('broll', [])):
            require(isinstance(br, dict), f'clip {key}.broll[{j}] must be an object')
            require(not any(k in br for k in ('source', 'file', 'path')), f'clip {key}.broll[{j}]: external sources are unsupported; source_start refers to the project source')
            at, duration, pos = [number(br.get(k), f'clip {key}.broll[{j}].{k}', 0) for k in ('at', 'duration', 'source_start')]
            require(duration > 0 and at >= previous_end - .00001 and at + duration <= c['duration'] + .00001, f'clip {key}.broll[{j}]: must be chronological, nonoverlapping and within the output duration')
            require(isinstance(br.get('freeze', False), bool), f'clip {key}.broll[{j}].freeze must be boolean')
            require(pos < meta['duration'] and (br.get('freeze') or pos + duration <= meta['duration'] + .00001), f'clip {key}.broll[{j}]: source range exceeds media duration')
            br.update(at=at, duration=duration, source_start=pos, crop=crop(br.get('crop', c['crop']), meta, f'clip {key}.broll[{j}].crop'), freeze=br.get('freeze', False))
            previous_end = at + duration
        previous_end = 0.0
        require(isinstance(c.get('labels', []), list), f'clip {key}.labels must be an array')
        for j, lab in enumerate(c.setdefault('labels', [])):
            require(isinstance(lab, dict), f'clip {key}.labels[{j}] must be an object')
            a = number(lab.get('output_start'), f'clip {key}.labels[{j}].output_start', 0)
            b = number(lab.get('output_end'), f'clip {key}.labels[{j}].output_end', 0)
            require(previous_end <= a < b <= c['duration'] + .00001, f'clip {key}.labels[{j}]: must be chronological, nonoverlapping and within the output')
            lab['text'] = nonempty(lab.get('text'), f'clip {key}.labels[{j}].text')
            previous_end = b
        cover = number(c.get('cover_output_time', min(3.0, max(0.0, c['duration'] - 1 / fps))), f'clip {key}.cover_output_time', 0)
        require(cover < c['duration'], f'clip {key}: cover_output_time must precede the final frame')
        c['cover_output_time'] = cover
        if c.get('opening_hook') is not None:
            hook = c['opening_hook']
            require(isinstance(hook, dict), f'clip {key}.opening_hook must be an object')
            hook['text'] = nonempty(hook.get('text'), f'clip {key}.opening_hook.text')
            d = number(hook.get('duration', 3), f'clip {key}.opening_hook.duration', 0)
            require(d > 0, f'clip {key}.opening_hook.duration must be positive')
            hook['duration'] = min(d, c['duration'])
    return project


class Layout:
    def __init__(self, project):
        self.project = project
        self.font = project['style']['font']
        self.bold_index = project['style']['font_bold_index']

    def face(self, size, bold=False):
        return ImageFont.truetype(self.font, size, index=self.bold_index if bold else 0)

    def fit(self, text, size, width, minimum=18, bold=False):
        while size >= minimum:
            font = self.face(size, bold)
            if font.getlength(text) <= width:
                return font
            size -= 1
        raise ProjectError(f'Text is too long for the layout: {text!r}')

    def text(self, draw, xy, text, size, color='#111111', width=932, bold=False):
        draw.text(xy, text, font=self.fit(text, size, width, bold=bold), fill=color, anchor='lt')

    def lines(self, text, size, width):
        lines, line, font = [], '', self.face(size)
        for token in re.findall(r'[A-Za-z0-9_.-]+|\n|\s+|.', text):
            if token == '\n':
                lines.append(line.rstrip()); line = ''; continue
            if font.getlength(token) > width:
                tokens = list(token)
            else:
                tokens = [token]
            for part in tokens:
                if line and font.getlength(line + part) > width:
                    lines.append(line.rstrip()); line = ''
                line += part
        if line:
            lines.append(line.rstrip())
        return lines

    def background(self, clip, target, opening=False):
        im = Image.new('RGB', SIZE, '#EFEFEF'); d = ImageDraw.Draw(im)
        d.rectangle((0, 0, 1080, 208), fill='#080808')
        with Image.open(self.project['style']['logo']) as original:
            logo = original.convert('RGBA')
        ratio = min(530 / logo.width, 170 / logo.height)
        logo = logo.resize((round(logo.width * ratio), round(logo.height * ratio)), Image.Resampling.LANCZOS)
        im.paste(logo, (22, 36), logo)
        self.text(d, (751, 94), self.project['event']['name'], 26, '#FFFFFF', 260)
        self.text(d, (751, 141), 'CASE / ' + clip['id'], 22, '#B8B8B8', 260)
        d.rectangle((72, 243, 79, 279), fill='#FF4900')
        self.text(d, (98, 247), clip['tag'], 26, width=900)
        heads, size = clip['headline'], 72
        if opening:
            text = clip['opening_hook']['text']
            while size >= 44:
                heads = self.lines(text, size, 930)
                if len(heads) <= 2:
                    break
                size -= 1
            require(len(heads) <= 2, f'clip {clip["id"]}: opening_hook needs shorter text (at most two lines)')
            if len(heads) == 1:
                heads = [heads[0], '']
        if heads[0]:
            self.text(d, (72, 315), heads[0], size, bold=True)
        if heads[1]:
            face = self.fit(heads[1], size, 930, bold=True)
            d.rectangle((63, 399, min(1018, 90 + face.getlength(heads[1])), 485), fill='#D4FF70')
            d.text((72, 407), heads[1], font=face, fill='#111111', anchor='lt')
        # Context/labels are ASS events, so they also survive the opening hook.
        d.rectangle((39, 549, 1041, 1361), fill='#777777')
        self.text(d, (72, 1394), clip['speaker'] + '  /  ' + clip['kind'], 29, '#555555')
        d.rectangle((60, 1470, 1020, 1650), fill='#FFFFFF')
        d.rectangle((60, 1470, 67, 1650), fill='#C18EFF')
        self.text(d, (74, 1700), '看点 /', 22, '#555555')
        self.text(d, (172, 1696), clip['takeaway'], 30, width=834)
        d.line((74, 1760, 1006, 1760), fill='#888888', width=2)
        self.text(d, (74, 1790), self.project['event']['footer'], 29)
        self.text(d, (74, 1850), self.project['event']['date'] + '  ·  ' + self.project['event']['name'], 22, '#555555', 700)
        for j, color in enumerate(['#FF4900', '#D4FF70', '#DEC7E2', '#00C0FF', '#00A158', '#C18EFF']):
            x = 790 + j * 36
            d.rectangle((x, 1840 - j * 11, x + 35, 1868), fill=color)
        im.save(target)


def stamp(seconds, srt=False):
    scale = 1000 if srt else 100
    units = round(seconds * scale)
    hours, units = divmod(units, 3600 * scale)
    minutes, units = divmod(units, 60 * scale)
    secs, fraction = divmod(units, scale)
    return f'{hours:02}:{minutes:02}:{secs:02},{fraction:03}' if srt else f'{hours}:{minutes:02}:{secs:02}.{fraction:02}'


def ass_text(text):
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')


def subtitle_files(project, clip, job, duration, layout):
    family = project['style']['font_family'].replace(',', ' ').replace('\n', ' ')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{family},52,&H00111111,&H00111111,&H00080808,&H00080808,0,0,0,0,100,100,0,0,1,0,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, srt = [], []
    for caption in clip['captions']:
        a, b = caption['start'], min(caption['end'], duration)
        if a >= duration:
            continue
        require(b > a, f'Invalid caption after sample truncation: {caption}')
        size = 52
        while size >= 40:
            lines = layout.lines(caption['text'], size, 884)
            if len(lines) <= 2:
                break
            size -= 1
        require(len(lines) <= 2, f'clip {clip["id"]}: caption is longer than two lines; split it: {caption["text"]!r}')
        rendered = r'\N'.join(ass_text(line) for line in lines)
        events.append(f'Dialogue: 0,{stamp(a)},{stamp(b)},Main,,0,0,0,,{{\\pos(540,1560)\\fs{size}}}{rendered}')
        srt.append(f'{len(srt)+1}\n{stamp(a, True)} --> {stamp(b, True)}\n' + '\n'.join(lines) + '\n')
    context_events, cursor = [], 0.0
    for lab in clip['labels']:
        if lab['output_start'] > cursor:
            context_events.append((cursor, lab['output_start'], clip['context']))
        context_events.append((lab['output_start'], lab['output_end'], lab['text']))
        cursor = lab['output_end']
    if cursor < duration:
        context_events.append((cursor, duration, clip['context']))
    for a, b, text in context_events:
        if a >= duration:
            continue
        size = layout.fit(text, 27, 928).size
        events.append(f'Dialogue: 1,{stamp(a)},{stamp(min(b,duration))},Main,,0,0,0,,{{\\an7\\pos(74,514)\\fs{size}}}{ass_text(text)}')
    # Visible disclosure distinguishes a frozen review frame from continuous action.
    for br in clip['broll']:
        if br['freeze'] and br['at'] < duration:
            events.append(f'Dialogue: 2,{stamp(br["at"])},{stamp(min(duration,br["at"]+br["duration"]))},Main,,0,0,0,,{{\\an9\\pos(1018,568)\\fs22\\1c&HFFFFFF&\\3c&H333333&\\bord2}}画面定格')
    (job / 'captions.ass').write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    (job / 'captions.srt').write_text('\n'.join(srt), encoding='utf-8')


def audio_chains(clip, index):
    chains, labels = [], []
    for j, (a, b) in enumerate(clip['keep']):
        duration = b - a
        fade = min(.005, duration / 4)
        chains.append(f'[0:{index}]atrim=start={a}:end={b},asetpts=N/SR/TB,afade=t=in:d={fade},afade=t=out:st={duration-fade}:d={fade}[a{j}]')
        labels.append(f'[a{j}]')
    chains.append(''.join(labels) + f'concat=n={len(labels)}:v=0:a=1[ca]')
    return chains


def run_logged(command, job, stem):
    (job / f'{stem}-command.json').write_text(json.dumps(command, ensure_ascii=False, indent=2) + '\n')
    log = job / f'{stem}.log'
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.run(command, cwd=job, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, check=False)
    require(process.returncode == 0, f'{stem} failed (exit {process.returncode}); inspect {log}\n{log.read_text(errors="replace")[-1400:]}')
    return log.read_text(errors='replace')


def source_input(project, clip):
    return ['-ss', str(clip['source_start']), '-t', str(clip['source_end'] - clip['source_start']), '-threads', str(THREADS), '-i', project['source']]


def measure(project, clip, duration, job, ffmpeg):
    chains = audio_chains(clip, project['source_metadata']['audio_index'])
    chains.append(f'[ca]atrim=duration={duration},asetpts=N/SR/TB,loudnorm=I=-16:LRA=11:TP=-1.5:print_format=json[a]')
    (job / 'measure-filter.txt').write_text(';\n'.join(chains))
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-y', *source_input(project, clip), '-filter_complex_threads', '1', '-filter_complex_script', 'measure-filter.txt', '-map', '[a]', '-f', 'null', '-']
    log = run_logged(cmd, job, 'loudness-pass1')
    matches = re.findall(r'\{\s*"input_i".*?\}', log, re.S)
    require(matches, f'clip {clip["id"]}: first-pass loudnorm did not return statistics')
    stats = json.loads(matches[-1])
    for key in ('input_i', 'input_lra', 'input_tp', 'input_thresh', 'target_offset'):
        require(key in stats and math.isfinite(float(stats[key])), f'clip {clip["id"]}: unusable loudness statistic {key}={stats.get(key)!r}; check for silence or an invalid audio track')
    (job / 'loudness.json').write_text(json.dumps(stats, indent=2) + '\n')
    return stats


def scale_crop(crop_values):
    x, y, w, h = crop_values
    return f'crop={w}:{h}:{x}:{y},scale=1000:810:force_original_aspect_ratio=decrease:flags=lanczos,pad=1000:810:(ow-iw)/2:(oh-ih)/2:color=0x000000,setsar=1'


def file_stem(clip):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', clip['name']).strip(' .')[:90]
    while len((clip['id'] + '_' + name).encode('utf-8')) > 220:
        name = name[:-1]
    return clip['id'] + '_' + (name or 'clip')


def render(project, clip, sample, ffmpeg):
    duration = min(sample, clip['duration']) if sample else clip['duration']
    mode = f'sample-{duration:g}s' if sample else 'final'
    job = Path(project['work_dir']) / 'jobs' / clip['id'] / mode
    job.mkdir(parents=True, exist_ok=True)
    fonts = job / 'fonts'; fonts.mkdir(exist_ok=True)
    font = Path(project['style']['font'])
    # System-font metadata can contain protected macOS flags; copy bytes only.
    shutil.copyfile(font, fonts / ('font' + font.suffix.lower()))
    layout = Layout(project)
    layout.background(clip, job / 'background.png')
    subtitle_files(project, clip, job, duration, layout)
    print(f'MEASURE {clip["id"]} {duration:.2f}s', flush=True)
    stats = measure(project, clip, duration, job, ffmpeg)
    fps = project['fps']
    cmd = [ffmpeg, '-hide_banner', '-v', 'warning', '-nostdin', '-y', *source_input(project, clip), '-loop', '1', '-framerate', str(fps), '-i', 'background.png']
    chains, video_labels = [], []
    for j, (a, b) in enumerate(clip['keep']):
        chains.append(f'[0:{project["source_metadata"]["video_index"]}]trim=start={a}:end={b},setpts=PTS-STARTPTS,fps={fps},{scale_crop(clip["segment_crops"][j])}[v{j}]')
        video_labels.append(f'[v{j}]')
    chains.append(''.join(video_labels) + f'concat=n={len(video_labels)}:v=1:a=0[screen]')
    chains.extend(audio_chains(clip, project['source_metadata']['audio_index']))
    next_input, background = 2, '1:v'
    if clip.get('opening_hook'):
        layout.background(clip, job / 'opening.png', opening=True)
        cmd += ['-loop', '1', '-framerate', str(fps), '-i', 'opening.png']
        chains.append(f'[1:v][{next_input}:v]overlay=0:0:enable=\'lt(t,{clip["opening_hook"]["duration"]})\'[background]')
        next_input += 1; background = 'background'
    picture = 'screen'
    for j, br in enumerate(clip['broll']):
        if br['at'] >= duration:
            continue
        br_duration = min(br['duration'], duration - br['at'])
        input_duration = max(2 / fps, .1) if br['freeze'] else br_duration
        cmd += ['-ss', str(br['source_start']), '-t', str(input_duration), '-threads', str(THREADS), '-i', project['source']]
        clock = f'fps={fps},setpts=PTS-STARTPTS+{br["at"]}/TB'
        if br['freeze']:
            clock = f'trim=end_frame=1,loop=loop=-1:size=1:start=0,setpts=N/({fps}*TB),trim=duration={br_duration},setpts=PTS+{br["at"]}/TB'
        chains.append(f'[{next_input}:{project["source_metadata"]["video_index"]}]{clock},{scale_crop(br["crop"])}[br{j}]')
        chains.append(f'[{picture}][br{j}]overlay=0:0:eof_action=pass:enable=\'gte(t,{br["at"]})*lt(t,{br["at"]+br_duration})\'[pictured{j}]')
        next_input += 1; picture = f'pictured{j}'
    chains.append(f'[{background}][{picture}]overlay=40:550:shortest=1,trim=duration={duration},setpts=N/({fps}*TB),ass=captions.ass:fontsdir=fonts,format=yuv420p[v]')
    chains.append(f'[ca]atrim=duration={duration},loudnorm=I=-16:LRA=11:TP=-1.5:measured_I={stats["input_i"]}:measured_LRA={stats["input_lra"]}:measured_TP={stats["input_tp"]}:measured_thresh={stats["input_thresh"]}:offset={stats["target_offset"]}:linear=true,aresample=48000,asetpts=N/SR/TB[a]')
    (job / 'render-filter.txt').write_text(';\n'.join(chains))
    cmd += ['-filter_complex_threads', '1', '-filter_complex_script', 'render-filter.txt', '-map', '[v]', '-map', '[a]', '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast', '-crf', '21', '-threads', str(THREADS), '-r', str(fps), '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-movflags', '+faststart', '-map_metadata', '-1', 'rendered.mp4']
    print(f'RENDER {clip["id"]} {mode}', flush=True)
    run_logged(cmd, job, 'render')
    cover_time = min(clip['cover_output_time'], max(0, duration - 1 / fps))
    run_logged([ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-y', '-ss', str(cover_time), '-i', 'rendered.mp4', '-frames:v', '1', '-vf', 'scale=in_range=limited:out_range=full', '-pix_fmt', 'yuvj420p', '-update', '1', 'cover.jpg'], job, 'cover')
    root = Path(project['work_dir']) / 'samples' if sample else Path(project['output_dir'])
    root.mkdir(parents=True, exist_ok=True)
    stem = file_stem(clip) + (f'_{duration:g}s-sample' if sample else '')
    targets = {'video': root / (stem + '.mp4'), 'subtitles': root / '字幕' / (stem + '.srt'), 'cover': root / '封面' / (stem + '.jpg')}
    for key, filename in [('video', 'rendered.mp4'), ('subtitles', 'captions.srt'), ('cover', 'cover.jpg')]:
        target = targets[key]; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job / filename, target)
    result = {'id': clip['id'], 'mode': mode, 'duration': duration, 'files': {k: str(v) for k, v in targets.items()}, 'job_dir': str(job), 'loudness': stats}
    (job / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('RENDERED ' + str(targets['video']), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Schema v1 project JSON; paths resolve from its directory')
    parser.add_argument('--clip', action='append', default=[], metavar='ID', help='Render this clip id; repeat to select more (default: all)')
    parser.add_argument('--sample', type=float, metavar='SECONDS', help='Render only this many initial seconds into work_dir/samples, leaving formal outputs untouched')
    parser.add_argument('--validate-only', action='store_true', help='Validate media, paths, timing, crops, subtitle coverage and layout; write no files')
    args = parser.parse_args(argv)
    try:
        require(args.sample is None or (math.isfinite(args.sample) and args.sample > 0), '--sample must be a positive finite number')
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        require(ffmpeg and ffprobe, 'ffmpeg and ffprobe must be available on PATH')
        project = load_project(args.project.expanduser().resolve(), ffprobe)
        wanted = set(args.clip)
        known = {c['id'] for c in project['clips']}
        require(not wanted - known, 'Unknown --clip id(s): ' + ', '.join(sorted(wanted - known)))
        clips = [c for c in project['clips'] if not wanted or c['id'] in wanted]
        # Layout validation is pure and applies even with --validate-only.
        layout = Layout(project)
        for c in clips:
            for text, size, width in [
                (project['event']['name'], 26, 260),
                ('CASE / ' + c['id'], 22, 260), (c['tag'], 26, 900),
                (c['speaker'] + '  /  ' + c['kind'], 29, 932),
                (c['takeaway'], 30, 834), (project['event']['footer'], 29, 932),
                (project['event']['date'] + '  ·  ' + project['event']['name'], 22, 700),
                (c['context'], 27, 928),
                *[(lab['text'], 27, 928) for lab in c['labels']],
            ]:
                layout.fit(text, size, width)
            for caption in c['captions']:
                require(len(layout.lines(caption['text'], 40, 884)) <= 2, f'clip {c["id"]}: caption exceeds two lines even at 40px; split {caption["text"]!r}')
            for text in c['headline']:
                layout.fit(text, 72, 932, bold=True)
            if c.get('opening_hook'):
                require(len(layout.lines(c['opening_hook']['text'], 44, 930)) <= 2, f'clip {c["id"]}: opening_hook exceeds two lines')
        print(json.dumps({'status': 'valid', 'clips': [{'id': c['id'], 'duration': c['duration'], 'captions': len(c['captions'])} for c in clips], 'source': project['source'], 'fps': project['fps']}, ensure_ascii=False))
        if args.validate_only:
            return 0
        work = Path(project['work_dir']); work.mkdir(parents=True, exist_ok=True)
        (work / 'project.normalized.json').write_text(json.dumps(project, ensure_ascii=False, indent=2) + '\n')
        results = [render(project, c, args.sample, ffmpeg) for c in clips]
        filename = 'sample-results.json' if args.sample else 'render-results.json'
        (work / filename).write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        return 0
    except (ProjectError, OSError, ValueError, KeyError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
