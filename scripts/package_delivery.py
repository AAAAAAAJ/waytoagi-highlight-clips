#!/usr/bin/env python3
"""Package existing highlight clips locally; this is not a media-quality check.

Usage: package_delivery.py --project project.json [--name 素材包名]
Relative project paths are resolved against the project JSON directory.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


class PackageError(Exception):
    pass


def number(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PackageError(f"{label} 必须是有限数值") from exc
    if not result.is_finite():
        raise PackageError(f"{label} 必须是有限数值")
    return result


def timecode(value) -> str:
    ms = int((number(value, '时间') * 1000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}.{ms:03}"


def safe_package_name(value: str) -> str:
    value = unicodedata.normalize('NFKC', value)
    if value.lower().endswith('.zip'):
        value = value[:-4]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    while len(value.encode('utf-8')) > 180:
        value = value[:-1]
    value = value.rstrip(' .') or '高光素材包'
    if re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', value):
        value = '素材包_' + value
    return value + '.zip'


def component(value, label: str) -> str:
    value = str(value)
    if not value or value in ('.', '..') or re.search(r'[/\\\x00-\x1f\x7f]', value):
        raise PackageError(f"{label} 必须是单一文件名组成部分，不得包含路径或控制字符")
    return value


def file_stem(clip) -> str:
    """Match render_clips.py; keep the unsanitized name for display copy."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', clip['name']).strip(' .')[:90]
    name = name or 'clip'
    while len((clip['id'] + '_' + name).encode('utf-8')) > 220:
        name = name[:-1]
    return clip['id'] + '_' + name


def project_path(base: Path, value, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PackageError(f"{label} 必须是非空路径字符串")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def prose(value) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        return '；'.join(str(item) for item in value if item is not None)
    return str(value)


def topics(value) -> str:
    return ' '.join(str(item) for item in value) if isinstance(value, list) else prose(value)


def crop_text(value, label: str) -> str:
    if value is None:
        return ''
    if not isinstance(value, list) or len(value) != 4:
        raise PackageError(f"{label} 应为 [x,y,w,h]")
    nums = [number(n, label) for n in value]
    if nums[0] < 0 or nums[1] < 0 or nums[2] <= 0 or nums[3] <= 0:
        raise PackageError(f"{label} 的坐标或尺寸无效")
    return ','.join(str(n) for n in value)


def probe_duration(path: Path, ffprobe: str) -> dict:
    cmd = [ffprobe, '-v', 'error', '-show_entries',
           'format=duration:stream=codec_type,duration,duration_ts,time_base',
           '-of', 'json', str(path)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackageError(f"无法读取 {path.name} 的媒体时长：{type(exc).__name__}") from exc
    if run.returncode:
        raise PackageError(f"ffprobe 无法读取 {path.name}（退出码 {run.returncode}）")
    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise PackageError(f"{path.name} 的 ffprobe 输出不是有效 JSON") from exc
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
    if not videos:
        raise PackageError(f"{path.name} 没有可读取的视频流")
    container = data.get('format', {}).get('duration')
    container_d = number(container, path.name + ' 容器时长') if container not in (None, 'N/A') else None
    video = videos[0]
    raw = video.get('duration')
    if raw not in (None, 'N/A'):
        duration = number(raw, path.name + ' 视频时长')
        basis = 'video_stream.duration'
    elif video.get('duration_ts') is not None and '/' in str(video.get('time_base', '')):
        a, b = video['time_base'].split('/', 1)
        divisor = number(b, path.name + ' time_base')
        if not divisor:
            raise PackageError(f"{path.name} 的 time_base 无效")
        duration = number(video['duration_ts'], path.name + ' duration_ts') * number(a, 'time_base') / divisor
        basis = 'video_stream.duration_ts*time_base'
    elif container_d is not None:
        duration, basis = container_d, 'format.duration'
    else:
        raise PackageError(f"无法确定 {path.name} 的实际时长")
    if duration <= 0:
        raise PackageError(f"{path.name} 的实际时长必须大于零")
    return {'duration': duration, 'basis': basis, 'container_duration': container_d}


def timeline(c: dict, actual_duration: Decimal, fps: Decimal) -> list[dict]:
    cid = c['id']
    start = number(c.get('source_start'), cid + '.source_start')
    if start < 0:
        raise PackageError(f"{cid}.source_start 不能小于零")
    end = number(c['source_end'], cid + '.source_end') if c.get('source_end') is not None else None
    keep = c.get('keep')
    if not isinstance(keep, list) or not keep:
        raise PackageError(f"{cid}.keep 必须是非空区间列表")
    crops = c.get('segment_crops')
    if crops is not None and (not isinstance(crops, list) or len(crops) != len(keep)):
        raise PackageError(f"{cid}.segment_crops 必须与 keep 一一对应")
    rows, cursor = [], Decimal('0')
    for i, pair in enumerate(keep, 1):
        if not isinstance(pair, list) or len(pair) != 2:
            raise PackageError(f"{cid}.keep[{i-1}] 应为 [开始,结束]")
        a, b = [number(v, f'{cid}.keep[{i-1}]') for v in pair]
        if a < 0 or b <= a or (end is not None and start + b > end + Decimal('0.001')):
            raise PackageError(f"{cid}.keep[{i-1}] 区间无效或超出源窗")
        length = b - a
        rows.append({'track': '原声与主画面', 'index': i, 'output_start': cursor,
                     'output_end': cursor + length, 'source_start': start + a,
                     'source_end': start + b, 'duration': length, 'source_kind': '连续片段',
                     'crop': crop_text((crops[i-1] if crops is not None else None) or c.get('crop'), cid + '.crop'),
                     'description': ''})
        cursor += length
    # This guards the source/output mapping; full media validation is a separate task.
    tolerance = max(Decimal('0.15'), Decimal('2') / fps)
    if abs(cursor - actual_duration) > tolerance:
        raise PackageError(f"{cid} 的 keep 时长 {cursor} 秒与实际视频 {actual_duration} 秒不符，无法可靠生成时间码")
    supplements = c.get('broll', [])
    if not isinstance(supplements, list):
        raise PackageError(f"{cid}.broll 应为列表")
    for i, br in enumerate(supplements, 1):
        if not isinstance(br, dict):
            raise PackageError(f"{cid}.broll[{i-1}] 应为对象")
        at = number(br.get('at'), cid + '.broll.at')
        length = number(br.get('duration'), cid + '.broll.duration')
        source = number(br.get('source_start'), cid + '.broll.source_start')
        if at < 0 or source < 0 or length <= 0 or at + length > actual_duration + Decimal('1') / fps:
            raise PackageError(f"{cid}.broll[{i-1}] 区间无效或超出成片")
        freeze = br.get('freeze', False)
        if not isinstance(freeze, bool):
            raise PackageError(f"{cid}.broll[{i-1}].freeze 应为布尔值")
        location = '开头' if at == 0 else '对应解说段'
        source_end = source if freeze else source + length
        evidence = (f"原片 {timecode(source)} 单帧，停留 {length:.2f} 秒"
                    if freeze else f"原片 {timecode(source)}—{timecode(source_end)}")
        description = (f"{location}配合同次演示{'静帧' if freeze else '画面'}，原声保留。"
                       f"片内 {timecode(at)}—{timecode(at+length)}；{evidence}。")
        rows.append({'track': '静帧' if freeze else '补充画面', 'index': i,
                     'output_start': at, 'output_end': at + length,
                     'source_start': source, 'source_end': source_end, 'duration': length,
                     'source_kind': '单帧' if freeze else '连续片段',
                     'crop': crop_text(br.get('crop', c.get('crop')), cid + '.broll.crop'),
                     'description': description})
    return rows


def xml_p(label: str, value: str) -> str:
    return f'<p><b>{escape(label)}：</b>{escape(value)}</p>'


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def package(project_file: Path, requested_name: str | None = None) -> dict:
    project_file = project_file.expanduser().resolve()
    try:
        project = json.loads(project_file.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"无法读取 project JSON：{type(exc).__name__}") from exc
    if not isinstance(project, dict):
        raise PackageError('project JSON 顶层必须是对象')
    base = project_file.parent
    output = project_path(base, project.get('output_dir', 'outputs'), 'output_dir')
    work = project_path(base, project.get('work_dir', 'work'), 'work_dir')
    # Resolve source according to the shared schema, but never copy or publish its path.
    if project.get('source') is not None:
        project_path(base, project['source'], 'source')
    fps = number(project.get('fps', 25), 'fps')
    if fps <= 0:
        raise PackageError('fps 必须大于零')
    event = project.get('event') or {}
    if not isinstance(event, dict):
        raise PackageError('event 必须是对象')
    event_name = prose(event.get('name')) or '高光剪辑'
    event_date, footer = prose(event.get('date')), prose(event.get('footer'))
    source_url = prose(project.get('source_url'))
    if source_url and not re.match(r'^https?://[^\s]+$', source_url, re.IGNORECASE):
        raise PackageError('source_url 应为 http/https 链接')
    clips = project.get('clips')
    if not isinstance(clips, list) or not clips:
        raise PackageError('clips 必须是非空列表')
    records, ids, file_names, missing = [], set(), set(), []
    for raw in clips:
        if not isinstance(raw, dict) or 'id' not in raw or 'name' not in raw:
            raise PackageError('每条 clip 必须包含 id 和 name')
        c = dict(raw)
        cid = component(c['id'], 'clip.id')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', cid):
            raise PackageError('clip.id 必须为 1–48 位字母、数字、下划线或连字符，并以字母或数字开头')
        if not isinstance(c['name'], str) or not c['name'].strip():
            raise PackageError('clip.name 必须是非空展示名称')
        name = c['name'].strip()
        c.update(id=cid, name=name)
        if cid in ids:
            raise PackageError(f'重复的 clip.id：{cid}')
        ids.add(cid)
        stem = file_stem(c)
        names = [stem + '.mp4', '封面/' + stem + '.jpg', '字幕/' + stem + '.srt']
        for member in names:
            if member in file_names:
                raise PackageError(f'重复的素材文件名：{member}')
            file_names.add(member)
            asset = output / member
            if not asset.is_file() or asset.stat().st_size == 0:
                missing.append(member)
        records.append({'clip': c, 'names': names})
    if missing:
        raise PackageError('缺少必需输出或文件为空：\n' + '\n'.join(missing))
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise PackageError('找不到 ffprobe，请先安装 FFmpeg 或配置 PATH')
    for item in records:
        item['probe'] = probe_duration(output / item['names'][0], ffprobe)
        item['timeline'] = timeline(item['clip'], item['probe']['duration'], fps)
    total = sum((r['probe']['duration'] for r in records), Decimal('0'))
    count = len(records)
    archive_name = safe_package_name(requested_name or f'{event_name}-{count}条高光素材包')
    title = f'{event_name}｜{count}条高光成片'
    txt = [title, '', f'成片数量：{count}条；实际视频总时长：{total:.2f}秒。',
           f'素材清单：{count}条MP4、{count}张JPG封面、{count}份SRT字幕、发布文案TXT、剪辑时间码CSV。',
           '同编号素材一一对应；时间码分别记录原声与主画面、补充画面和静帧。']
    xml = [f'<title>{escape(title)}</title>',
           f'<p>成片共 {count} 条，实际视频总时长 {total:.2f} 秒。每条包含成片、封面与字幕，素材包另附发布文案及剪辑时间码。</p>']
    if event_date:
        txt.append('日期：' + event_date)
        xml.append(xml_p('日期', event_date))
    if footer:
        txt.append('系列说明：' + footer)
        xml.append(xml_p('系列说明', footer))
    if source_url:
        txt.append('原回放：' + source_url)
        xml.append(f'<p><b>来源：</b><a href="{escape(source_url, quote=True)}">原回放</a></p>')
    xml.extend(['<h1>完整素材包</h1>', '<p>完整素材包下载</p>'])
    csv_buffer = io.StringIO(newline='')
    writer = csv.writer(csv_buffer)
    writer.writerow(['编号', '名称', '讲者', '主题', '成片总时长_秒', '轨道类型', '片段序号',
                     '成片入点', '成片出点', '成片入点_秒', '成片出点_秒',
                     '原片入点', '原片出点', '原片入点_秒', '原片出点_秒',
                     '片段时长_秒', '来源形式', '画面裁切_xywh',
                     '视频文件', '封面文件', '字幕文件', '原片链接', '说明'])
    for item in records:
        c, names, actual = item['clip'], item['names'], item['probe']['duration']
        speaker = prose(c.get('speaker'))
        context = prose(c.get('context', c.get('tag', '')))
        caption = prose(c.get('publish_caption', c.get('caption', '')))
        note, topic_text = prose(c.get('note')), topics(c.get('topics', []))
        heading = c['id'] + '｜' + c['name']
        main = [r for r in item['timeline'] if r['track'] == '原声与主画面']
        source_spans = '；'.join(f"{timecode(r['source_start'])}—{timecode(r['source_end'])}" for r in main)
        txt.extend(['', heading, f'成片时长：{actual:.2f}秒｜讲者：{speaker}｜主题：{context}',
                    '发布文案：' + caption, '话题：' + topic_text, '内容说明：' + note,
                    '原声与主画面来源区间：' + source_spans,
                    '成片：' + names[0], '封面：' + names[1], '字幕：' + names[2]])
        xml.extend([f'<h1>{escape(heading)}</h1>',
                    f'<p><b>成片时长：</b>{actual:.2f} 秒。<b>讲者：</b>{escape(speaker)}。<b>主题：</b>{escape(context)}</p>',
                    xml_p('发布文案', caption), xml_p('话题', topic_text), xml_p('内容说明', note),
                    f"<p>成片 {escape(heading)}</p>", f"<p>封面 {escape(heading)}</p>",
                    xml_p('原声与主画面来源区间', source_spans)])
        for row in item['timeline']:
            if row['description']:
                txt.append(row['track'] + '：' + row['description'])
                xml.append(xml_p(row['track'], row['description']))
            writer.writerow([c['id'], c['name'], speaker, context, f'{actual:.3f}', row['track'], row['index'],
                             timecode(row['output_start']), timecode(row['output_end']),
                             f"{row['output_start']:.3f}", f"{row['output_end']:.3f}",
                             timecode(row['source_start']), timecode(row['source_end']),
                             f"{row['source_start']:.3f}", f"{row['source_end']:.3f}", f"{row['duration']:.3f}",
                             row['source_kind'], row['crop'], *names, source_url, row['description'] or note])
    xml_content = '\n'.join(xml) + '\n'
    ET.fromstring('<root>' + xml_content + '</root>')
    # Only publication metadata and relative asset names are serialized; no project dump.
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.package-build-', dir=output) as temp:
        stage = Path(temp)
        txt_path = stage / '发布文案与素材说明.txt'
        csv_path = stage / '剪辑时间码.csv'
        txt_path.write_text('\n'.join(txt) + '\n', encoding='utf-8')
        csv_path.write_text(csv_buffer.getvalue(), encoding='utf-8-sig')
        assets = [(output / name, name) for item in records for name in item['names']]
        assets.extend([(txt_path, txt_path.name), (csv_path, csv_path.name)])
        manifest_files = [{'name': name, 'size': path.stat().st_size, 'sha256': digest(path)} for path, name in assets]
        archive = stage / archive_name
        with zipfile.ZipFile(archive, 'w', allowZip64=True) as z:
            for path, name in assets:
                compression = zipfile.ZIP_STORED if path.suffix.lower() in ('.mp4', '.jpg') else zipfile.ZIP_DEFLATED
                z.write(path, name, compress_type=compression, compresslevel=6)
        with zipfile.ZipFile(archive) as z:
            if len(z.infolist()) != 3 * count + 2:
                raise PackageError('素材包文件数量与白名单不符')
            for record in manifest_files:
                hasher = hashlib.sha256()
                with z.open(record['name']) as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        hasher.update(chunk)
                if z.getinfo(record['name']).file_size != record['size'] or hasher.hexdigest() != record['sha256']:
                    raise PackageError(f"打包期间文件发生变化：{record['name']}")
        manifest = {
            'schema_version': 1, 'status': 'packaged', 'clip_count': count,
            'duration_seconds': float(total), 'event': {'name': event_name, 'date': event_date, 'footer': footer},
            'source_url': source_url,
            'package': {'name': archive_name, 'size': archive.stat().st_size, 'sha256': digest(archive),
                        'file_count': len(manifest_files)},
            'files': manifest_files,
            'videos': [{'id': i['clip']['id'], 'name': i['names'][0], 'duration_seconds': float(i['probe']['duration']),
                        'duration_basis': i['probe']['basis'],
                        'container_duration_seconds': float(i['probe']['container_duration']) if i['probe']['container_duration'] is not None else None}
                       for i in records],
            'media_verification': 'not_performed_by_packager',
        }
        for path in (txt_path, csv_path, archive):
            os.replace(path, output / path.name)
        write_atomic(work / 'delivery.xml', xml_content)
        write_atomic(work / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'status': 'packaged', 'clips': count, 'duration_seconds': float(total),
            'archive_items': 3 * count + 2, 'archive': str(output / archive_name),
            'delivery_xml': str(work / 'delivery.xml'), 'manifest': str(work / 'manifest.json'),
            'media_verification': 'not_performed_by_packager'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--name', help='素材包名称；非法文件名字符会替换为下划线')
    args = parser.parse_args()
    try:
        result = package(args.project, args.name)
    except (PackageError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
