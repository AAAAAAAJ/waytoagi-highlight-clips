#!/usr/bin/env python3
"""Apply semantically reviewed speech cuts to a new render project, without media I/O.

Cuts and optional replacement captions use the original assembled output seconds.
This tool does not identify filler words, evaluate meaning, run ASR, or edit media.
Requires only Python's standard library. Run render_clips.py on the new project.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys


EPS = 1e-7
KINDS = {'filler', 'repetition', 'restart', 'redundancy', 'waiting'}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z')
REPORT_NAME = 'speech-edit-report.json'
ROOT = Path(__file__).resolve().parents[1]


class RefineError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RefineError(message)


def number(value, label):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0, f'{label}: expected a finite nonnegative number')
    return float(value)


def text(value, label, allow_empty=False):
    require(isinstance(value, str) and (allow_empty or value.strip()), f'{label}: expected text')
    return value.strip()


def rounded(value):
    return round(value, 9)


def frames(value, fps, label):
    value = number(value, label)
    result = round(value * fps)
    require(abs(value * fps - result) < .00001,
            f'{label}: must be on the 1/{fps}-second frame grid; choose the edit boundary deliberately')
    return result


def load_json(path):
    try:
        raw = path.read_bytes()
        result = json.loads(raw)
    except (OSError, ValueError) as error:
        raise RefineError(f'Cannot read JSON {path}: {error}') from error
    require(isinstance(result, dict), f'{path}: JSON root must be an object')
    require(type(result.get('schema_version')) is int and result['schema_version'] == 1,
            f'{path}: schema_version must be 1')
    return result, hashlib.sha256(raw).hexdigest()


def resolve(base, value, label):
    path = Path(text(value, label)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def inside(path, directory):
    return path == directory or directory in path.parents


def related(a, b):
    return inside(a, b) or inside(b, a)


def absent(path):
    # lexists behaviour also rejects broken symlinks.
    require(not path.exists() and not path.is_symlink(), f'Refusing to overwrite existing destination: {path}')


def configure_paths(project, project_file, edits_file, out_file, output_dir, work_dir):
    """Resolve old inputs first; keep all new writes outside old render directories."""
    base = project_file.parent
    old_dirs = [resolve(base, project.get(k), k) for k in ('output_dir', 'work_dir')]
    source = resolve(base, project.get('source'), 'source')
    require(source.is_file(), f'Source file not found: {source}')
    project['source'] = str(source)
    style = project.setdefault('style', {})
    require(isinstance(style, dict), 'style must be an object')
    assets = [source]
    for key in ('font', 'logo'):
        if style.get(key):
            asset = resolve(base, style[key], 'style.' + key)
        elif key == 'logo':
            asset = (ROOT / 'assets' / 'brand-logo-white.png').resolve()
        else:
            continue  # Preserve renderer font auto-detection when no font was specified.
        require(asset.is_file(), f'{key} file not found: {asset}')
        style[key] = str(asset)
        assets.append(asset)
    report_file = out_file.parent / REPORT_NAME
    require(out_file != report_file, f'--out cannot use the reserved report name {REPORT_NAME}')
    protected = [project_file, edits_file, *assets]
    for target in (out_file, report_file):
        require(target not in protected, f'Refusing to overwrite an input: {target}')
        require(not any(inside(target, old) for old in old_dirs),
                f'New project/report must be outside old output_dir and work_dir: {target}')
        absent(target)
    destinations = [resolve(out_file.parent, value or default, key)
                    for key, value, default in [('output_dir', output_dir, 'output'), ('work_dir', work_dir, 'work')]]
    require(not related(*destinations), 'New output_dir and work_dir must be separate, non-nested directories')
    for key, destination in zip(('output_dir', 'work_dir'), destinations):
        require(not any(related(destination, old) for old in old_dirs),
                f'{key}: refusing to reuse or overlap old output_dir/work_dir: {destination}')
        require(not any(inside(p, destination) for p in [*protected, out_file, report_file]),
                f'{key}: cannot contain project/report/input files: {destination}')
        require(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())),
                f'{key}: destination must be absent or an empty directory: {destination}')
        project[key] = str(destination)
    return report_file


def validate_crop(value, label):
    if value is None:
        return
    require(isinstance(value, list) and len(value) == 4
            and all(type(v) is int and v >= 0 and v % 2 == 0 for v in value)
            and value[2] > 0 and value[3] > 0, f'{label}: expected even [x,y,width,height] with positive size')


def old_timeline(clip, fps):
    cid = clip['id']
    start = number(clip.get('source_start'), f'{cid}.source_start')
    end = number(clip.get('source_end'), f'{cid}.source_end')
    require(end > start, f'{cid}: source_end must be greater than source_start')
    keep = clip.get('keep')
    require(isinstance(keep, list) and keep, f'{cid}.keep must be a nonempty array')
    validate_crop(clip.get('crop'), f'{cid}.crop')
    crops = clip.get('segment_crops', [None] * len(keep))
    require(isinstance(crops, list) and len(crops) == len(keep), f'{cid}.segment_crops must match keep length')
    spans, cursor = [], 0
    for i, (pair, crop) in enumerate(zip(keep, crops)):
        require(isinstance(pair, list) and len(pair) == 2, f'{cid}.keep[{i}] must be [start,end]')
        a, b = [frames(v, fps, f'{cid}.keep[{i}]') for v in pair]
        require(a < b and b / fps <= end - start + EPS, f'{cid}.keep[{i}]: invalid source-window range')
        validate_crop(crop, f'{cid}.segment_crops[{i}]')
        spans.append({'index': i, 'start': cursor, 'end': cursor + b - a, 'window_start': a, 'window_end': b,
                      'crop': copy.deepcopy(crop), 'effective_crop': copy.deepcopy(crop if crop is not None else clip.get('crop'))})
        cursor += b - a
    return spans, cursor


def validate_cuts(edit, total, fps, cid):
    cuts = edit.get('cuts', [])
    require(isinstance(cuts, list), f'{cid}.cuts must be an array')
    result = []
    for i, raw in enumerate(cuts):
        require(isinstance(raw, dict), f'{cid}.cuts[{i}] must be an object')
        kind = raw.get('kind')
        require(isinstance(kind, str) and kind in KINDS, f'{cid}.cuts[{i}].kind must be one of {sorted(KINDS)}')
        a, b = [frames(raw.get(k), fps, f'{cid}.cuts[{i}].{k}') for k in ('start', 'end')]
        require(a < b <= total, f'{cid}.cuts[{i}]: must have positive duration inside the old output')
        result.append({'index': i, 'start': a, 'end': b, 'kind': kind,
                       'text': text(raw.get('text'), f'{cid}.cuts[{i}].text', allow_empty=kind == 'waiting'),
                       'reason': text(raw.get('reason'), f'{cid}.cuts[{i}].reason')})
    result.sort(key=lambda c: (c['start'], c['end']))
    for a, b in zip(result, result[1:]):
        require(b['start'] >= a['end'], f'{cid}: cuts overlap at entries {a["index"]} and {b["index"]}')
    require(sum(c['end'] - c['start'] for c in result) < total, f'{cid}: refusing to delete the entire clip')
    return result


def retained_spans(cuts, total, fps):
    result, cursor, output = [], 0, 0
    for cut in [*cuts, {'start': total, 'end': total}]:
        if cursor < cut['start']:
            end = cut['start']
            result.append({'start': cursor / fps, 'end': end / fps, 'new_start': output / fps})
            output += end - cursor
        cursor = cut['end']
    return result, output / fps


def intersections(start, end, retained):
    for span in retained:
        a, b = max(start, span['start']), min(end, span['end'])
        if b > a + EPS:
            yield a, b, span['new_start'] + a - span['start']


def mapped_point(point, retained):
    return rounded(sum(max(0.0, min(point, r['end']) - r['start']) for r in retained))


def initial_captions(clip, duration, override=None):
    """Expand every old keep occurrence before testing cuts; no source deduplication."""
    cid = clip['id']
    items = override if override is not None else clip.get('captions', [])
    timebase = 'output' if override is not None else clip.get('caption_timebase', 'window')
    require(timebase in ('window', 'output'), f'{cid}.caption_timebase must be window or output')
    require(isinstance(items, list), f'{cid}.captions must be an array')
    mapped = []
    for i, raw in enumerate(items):
        label = f'{cid}.captions[{i}]'
        require(isinstance(raw, dict), f'{label} must be an object')
        s, e = [number(raw.get(k), f'{label}.{k}') for k in ('start', 'end')]
        words = text(raw.get('text'), f'{label}.text')
        require(e > s, f'{label} must have positive duration')
        require(isinstance(raw.get('editorial', False), bool), f'{label}.editorial must be boolean')
        if timebase == 'output':
            groups = [{'start': s, 'end': e}]
        else:
            cursor, covered = s, []
            for a, b in sorted(clip['keep']):
                if b <= cursor or a >= e:
                    continue
                require(a <= cursor + EPS, f'{label} {words!r}: original window caption crosses unkept speech; supply reviewed output captions')
                cursor = max(cursor, min(b, e))
                covered.append((a, b))
            require(cursor >= e - EPS, f'{label} {words!r}: original window caption lies outside kept speech; supply reviewed output captions')
            groups, cursor = [], 0.0
            for a, b in clip['keep']:
                left, right = max(s, a), min(e, b)
                if right > left + EPS:
                    piece = {'start': cursor + left - a, 'end': cursor + right - a, 'source_start': left, 'source_end': right}
                    if groups and abs(groups[-1]['end'] - piece['start']) < EPS and abs(groups[-1]['source_end'] - left) < EPS:
                        groups[-1].update(end=piece['end'], source_end=right)
                    else:
                        groups.append(piece)
                cursor += b - a
            require(groups and all(abs(g['source_start'] - s) < EPS and abs(g['source_end'] - e) < EPS for g in groups),
                    f'{label} {words!r}: a repeated/trimmed occurrence contains only part of this caption; split by retained words or supply reviewed output captions')
        for group in groups:
            require(group['end'] <= duration + EPS, f'{label}: exceeds the old output duration')
            mapped.append({'start': rounded(group['start']), 'end': rounded(group['end']), 'text': words,
                           **({'editorial': True} if raw.get('editorial') else {})})
    mapped.sort(key=lambda c: (c['start'], c['end']))
    for a, b in zip(mapped, mapped[1:]):
        require(b['start'] >= a['end'] - EPS, f'{cid}: overlapping old-output captions {a["text"]!r} / {b["text"]!r}')
    return mapped


def refine_captions(captions, retained, cid):
    result, removed = [], []
    for i, caption in enumerate(captions):
        a, b = caption['start'], caption['end']
        pieces = list(intersections(a, b, retained))
        kept = sum(end - start for start, end, _ in pieces)
        if kept < EPS:
            removed.append({'index': i, **caption})
            continue
        require(abs(kept - (b - a)) < EPS,
                f'{cid}.captions[{i}] {caption["text"]!r} partially crosses a cut at old output [{a},{b}]. '
                'Provide a complete replacement captions list in edits, split by the actual retained words. '
                'Do not merely shorten timestamps to hide deleted words.')
        result.append({**caption, 'start': mapped_point(a, retained), 'end': mapped_point(b, retained)})
    return result, removed


def refine_overlays(clip, retained, duration):
    reports = {}
    for field in ('broll', 'labels'):
        original = clip.get(field, [])
        require(isinstance(original, list), f'{clip["id"]}.{field} must be an array')
        result, changes, previous = [], [], 0.0
        for i, raw in enumerate(original):
            label = f'{clip["id"]}.{field}[{i}]'
            require(isinstance(raw, dict), f'{label} must be an object')
            if field == 'broll':
                require(not any(k in raw for k in ('source', 'file', 'path')), f'{label}: B-roll must use the project source')
                a = number(raw.get('at'), label + '.at')
                b = a + number(raw.get('duration'), label + '.duration')
                pos = number(raw.get('source_start'), label + '.source_start')
                require(isinstance(raw.get('freeze', False), bool), f'{label}.freeze must be boolean')
                validate_crop(raw.get('crop'), label + '.crop')
            else:
                a, b = [number(raw.get(k), label + '.' + k) for k in ('output_start', 'output_end')]
                text(raw.get('text'), label + '.text')
            require(a >= previous - EPS and a < b <= duration + EPS, f'{label}: expected chronological, nonoverlapping intervals within the old output')
            previous = b
            indices = []
            for left, right, new_start in intersections(a, b, retained):
                item = copy.deepcopy(raw)
                if field == 'broll':
                    item.update(at=rounded(new_start), duration=rounded(right - left),
                                source_start=rounded(pos if raw.get('freeze') else pos + left - a))
                else:
                    item.update(output_start=rounded(new_start), output_end=rounded(new_start + right - left))
                indices.append(len(result))
                result.append(item)
            changes.append({'before_index': i, 'after_indices': indices,
                            'action': 'removed' if not indices else 'split' if len(indices) > 1 else 'mapped'})
        if field in clip:
            clip[field] = result
        reports[field] = {'before_count': len(original), 'after_count': len(result), 'changes': changes}
    return reports


def source_spans(start, end, spans, source_start, fps):
    result = []
    for span in spans:
        a, b = max(start, span['start']), min(end, span['end'])
        if b > a:
            window_a = (span['window_start'] + a - span['start']) / fps
            window_b = (span['window_start'] + b - span['start']) / fps
            result.append({'keep_index': span['index'], 'before_output': [a / fps, b / fps],
                           'window': [rounded(window_a), rounded(window_b)],
                           'source_absolute': [rounded(source_start + window_a), rounded(source_start + window_b)]})
    return result


def refine_clip(original, edit, fps):
    clip = copy.deepcopy(original)
    cid = clip['id']
    spans, total = old_timeline(clip, fps)
    duration = total / fps
    cuts = validate_cuts(edit, total, fps, cid)
    retained, after_duration = retained_spans(cuts, total, fps)
    override = edit.get('captions')
    require('captions' not in edit or isinstance(override, list), f'{cid}: replacement captions must be a complete array')
    before_captions = initial_captions(clip, duration, override)
    captions, removed = refine_captions(before_captions, retained, cid)
    keep, crops, mapping = [], [], []
    for span in spans:
        for a, b, new_start in intersections(span['start'] / fps, span['end'] / fps, retained):
            window_start = span['window_start'] / fps + a - span['start'] / fps
            window_end = window_start + b - a
            keep.append([rounded(window_start), rounded(window_end)])
            crops.append(copy.deepcopy(span['crop']))
            mapping.append({'keep_index_before': span['index'], 'keep_index_after': len(keep) - 1,
                            'before_output': [rounded(a), rounded(b)],
                            'after_output': [rounded(new_start), rounded(new_start + b - a)],
                            'source_absolute': [rounded(clip['source_start'] + window_start), rounded(clip['source_start'] + window_end)],
                            'effective_crop': span['effective_crop']})
    clip.update(keep=keep, duration=rounded(after_duration), captions=captions, caption_timebase='output')
    if 'segment_crops' in clip:
        clip['segment_crops'] = crops
    overlays = refine_overlays(clip, retained, duration)
    old_cover = number(original.get('cover_output_time', min(3.0, max(0.0, duration - 1 / fps))), f'{cid}.cover_output_time')
    require(old_cover < duration, f'{cid}.cover_output_time must be before the old output end')
    selected, choice = old_cover, 'retained'
    if not any(r['start'] <= old_cover < r['end'] for r in retained):
        following = next((r for r in retained if r['start'] > old_cover), None)
        selected = following['start'] if following else retained[-1]['end'] - 1 / fps
        choice = 'next_retained_frame' if following else 'previous_retained_frame'
    clip['cover_output_time'] = mapped_point(selected, retained)
    reviews = []
    if cuts:
        reviews.append('Listen to every new cut for clipped syllables, breathing, clicks, and retained meaning; this script does not verify semantics or audio.')
        reviews.append('Review the first 3 seconds, title promise, attribution, and visual continuity after rendering.')
    if choice != 'retained':
        reviews.append('Cover frame was replaced; verify it still represents the result.')
    hook_report = None
    if original.get('opening_hook') is not None:
        hook = copy.deepcopy(original['opening_hook'])
        require(isinstance(hook, dict), f'{cid}.opening_hook must be an object')
        text(hook.get('text'), f'{cid}.opening_hook.text')
        old_hook = number(hook.get('duration', 3), f'{cid}.opening_hook.duration')
        require(old_hook > 0, f'{cid}.opening_hook.duration must be positive')
        effective = min(old_hook, duration)
        new_hook = mapped_point(effective, retained)
        if new_hook <= EPS:
            clip.pop('opening_hook', None)
            reviews.append('Opening hook interval was completely removed. Redesign the opening title and evidence before rendering.')
        else:
            hook['duration'] = new_hook
            clip['opening_hook'] = hook
        hook_report = {'before_duration': old_hook, 'before_effective_duration': effective,
                       'after_duration': new_hook, 'removed': new_hook <= EPS}
    cut_reports, cut_points = [], {}
    for cut in cuts:
        a, b = cut['start'], cut['end']
        point = mapped_point(a / fps, retained)
        cut_reports.append({'edit_index': cut['index'], 'before_output': [a / fps, b / fps],
                            'duration': (b - a) / fps, 'kind': cut['kind'], 'text': cut['text'], 'reason': cut['reason'],
                            'source_intervals': source_spans(a, b, spans, clip['source_start'], fps),
                            'after_output_cut': point})
        key = str(point)
        if key not in cut_points:
            cut_points[key] = {'output_time': point, 'edit_indices': [],
                               'position': 'start' if point < EPS else 'end' if abs(point - after_duration) < EPS else 'join'}
        cut_points[key]['edit_indices'].append(cut['index'])
    return clip, {'id': cid, 'before_duration': duration, 'after_duration': rounded(after_duration),
                  'deleted_duration': rounded(duration - after_duration), 'cuts': cut_reports,
                  'new_cut_points': list(cut_points.values()), 'retained_segments': mapping,
                  'captions': {'input_count': len(original.get('captions', [])), 'replacement_used': override is not None,
                               'before_output_count': len(before_captions), 'after_count': len(captions), 'removed': removed,
                               'before_output': before_captions, 'after_output': captions},
                  **overlays, 'cover': {'before_time': old_cover, 'selected_before_time': rounded(selected),
                                       'after_time': clip['cover_output_time'], 'selection': choice},
                  'opening_hook': hook_report, 'manual_review': reviews}


def refine(project_file, edits_file, out_file, output_dir=None, work_dir=None):
    # Check the supplied pathname before resolving links: a dangling destination
    # symlink is already occupied even though its eventual target does not exist.
    requested_out = Path(out_file).expanduser().absolute()
    absent(requested_out)
    project_file, edits_file = [Path(p).expanduser().resolve() for p in (project_file, edits_file)]
    out_file = requested_out.resolve()
    project, project_hash = load_json(project_file)
    edits, edits_hash = load_json(edits_file)
    require(edits.get('timebase') == 'output', 'edits.timebase must be "output" (before speech refinement)')
    fps = project.get('fps', 25)
    require(type(fps) is int and 1 <= fps <= 60, 'project.fps must be an integer from 1 to 60')
    clips = project.get('clips')
    require(isinstance(clips, list) and clips, 'project.clips must be a nonempty array')
    ids = set()
    for clip in clips:
        require(isinstance(clip, dict), 'project.clips entries must be objects')
        cid = clip.get('id')
        require(isinstance(cid, str) and ID.fullmatch(cid), f'Invalid clip id: {cid!r}')
        require(cid not in ids, f'Duplicate project clip id: {cid}')
        ids.add(cid)
    edit_clips = edits.get('clips')
    require(isinstance(edit_clips, list) and edit_clips, 'edits.clips must be a nonempty array')
    by_id = {}
    for edit in edit_clips:
        require(isinstance(edit, dict), 'edits.clips entries must be objects')
        cid = edit.get('id')
        require(isinstance(cid, str) and cid in ids, f'Unknown edits clip id: {cid!r}')
        require(cid not in by_id, f'Duplicate edits clip id: {cid}')
        require('cuts' in edit, f'{cid}: cuts array is required (use [] for caption-only review)')
        by_id[cid] = edit
    report_file = configure_paths(project, project_file, edits_file, out_file, output_dir, work_dir)
    result, reports = [], []
    for clip in clips:
        cleaned, report = refine_clip(clip, by_id.get(clip['id'], {'cuts': []}), fps)
        result.append(cleaned)
        reports.append(report)
    project['clips'] = result
    report = {'schema_version': 1, 'status': 'project_refined_media_not_rendered', 'timebase': 'before_output', 'fps': fps,
              'semantic_review': 'Deletion decisions were supplied by the editor. This script did not classify speech or verify their meaning.',
              'inputs': {'project': str(project_file), 'project_sha256': project_hash, 'edits': str(edits_file), 'edits_sha256': edits_hash},
              'outputs': {'project': str(out_file), 'report': str(report_file), 'output_dir': project['output_dir'], 'work_dir': project['work_dir']},
              'before_duration': rounded(sum(c['before_duration'] for c in reports)),
              'after_duration': rounded(sum(c['after_duration'] for c in reports)),
              'deleted_duration': rounded(sum(c['deleted_duration'] for c in reports)), 'clips': reports}
    payloads = [(out_file, project), (report_file, report)]
    # All semantic/timing/path validation finishes before any directory or file is created.
    serialized = [(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n') for path, data in payloads]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, payload in serialized:
            with path.open('x', encoding='utf-8') as stream:
                created.append(path)
                stream.write(payload)
    except OSError:
        for path in created:
            path.unlink()
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Existing schema-v1 project; never modified')
    parser.add_argument('--edits', required=True, type=Path, help='Reviewed output-time cuts and optional complete replacement captions')
    parser.add_argument('--out', required=True, type=Path, help='New project JSON; existing files are rejected')
    parser.add_argument('--output-dir', help='New media directory, relative to --out parent (default: output)')
    parser.add_argument('--work-dir', help='New render work directory, relative to --out parent (default: work)')
    args = parser.parse_args(argv)
    try:
        report = refine(args.project, args.edits, args.out, args.output_dir, args.work_dir)
    except (RefineError, OSError, TypeError, KeyError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({'status': report['status'], 'before_duration': report['before_duration'],
                      'after_duration': report['after_duration'], 'deleted_duration': report['deleted_duration'],
                      **report['outputs']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
