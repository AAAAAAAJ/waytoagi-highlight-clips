"""Deterministic timeline and no-overwrite checks; no media tools are invoked.

Run: python3 -m unittest discover -s tests -p 'test_refine_speech.py'
Set SPEECH_TEST_TMP to place temporary fixtures in a chosen task directory.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('refine_speech', ROOT / 'scripts' / 'refine_speech.py')
speech = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speech)


def cut(a, b, kind='filler', words='嗯', reason='已听原音，删除不影响句意'):
    return {'start': a, 'end': b, 'kind': kind, 'text': words, 'reason': reason}


def caption(a, b, words='完整原话'):
    return {'start': a, 'end': b, 'text': words}


def clip(**changes):
    value = {'id': '01', 'name': '案例', 'tag': '实测', 'headline': ['开头', '结果'],
             'context': '现场演示', 'speaker': '讲者', 'kind': '实测', 'takeaway': '展示结果',
             'source_start': 100.12, 'source_end': 130.12, 'keep': [[0, 10]],
             'crop': [0, 0, 1920, 1080], 'caption_timebase': 'output', 'captions': [],
             'cover_output_time': 1}
    value.update(changes)
    return value


class TimelineTests(unittest.TestCase):
    def test_source_offset_and_cut_crossing_keep(self):
        original = clip(keep=[[2, 5], [10, 14]], segment_crops=[None, [100, 0, 1600, 1080]])
        untouched = copy.deepcopy(original)
        result, report = speech.refine_clip(original, {'cuts': [cut(2, 4)]}, 25)
        self.assertEqual(original, untouched)
        self.assertEqual(result['keep'], [[2, 4], [11, 14]])
        self.assertEqual(result['segment_crops'], [None, [100, 0, 1600, 1080]])
        self.assertEqual(report['retained_segments'][0]['effective_crop'], [0, 0, 1920, 1080])
        self.assertEqual(report['cuts'][0]['source_intervals'], [
            {'keep_index': 0, 'before_output': [2, 3], 'window': [4, 5], 'source_absolute': [104.12, 105.12]},
            {'keep_index': 1, 'before_output': [3, 4], 'window': [10, 11], 'source_absolute': [110.12, 111.12]}])
        self.assertEqual(result['duration'], 5)
        self.assertEqual(report['new_cut_points'][0]['output_time'], 2)

    def test_repeated_source_only_target_output_occurrence_deleted(self):
        original = clip(keep=[[10, 12], [0, 2], [10, 12]], caption_timebase='window',
                        captions=[caption(10, 11, '再来一次'), caption(11, 12, '完成')])
        result, report = speech.refine_clip(original, {'cuts': [cut(1, 3)]}, 25)
        self.assertEqual(result['keep'], [[10, 11], [1, 2], [10, 12]])
        self.assertEqual(result['captions'], [caption(0, 1, '再来一次'), caption(2, 3, '再来一次'), caption(3, 4, '完成')])
        self.assertEqual(report['captions']['before_output_count'], 4)
        self.assertEqual(len(report['captions']['removed']), 1)

    def test_caption_boundary_contacts_and_adjacent_cuts(self):
        original = clip(captions=[caption(0, 2, '保留前句'), caption(2, 4, '完整删除'), caption(4, 6, '保留后句')])
        result, report = speech.refine_clip(original, {'cuts': [cut(3, 4), cut(2, 3)]}, 25)
        self.assertEqual(result['captions'], [caption(0, 2, '保留前句'), caption(2, 4, '保留后句')])
        self.assertEqual(len(report['new_cut_points']), 1)
        self.assertEqual(len(report['captions']['removed']), 1)
        self.assertEqual(report['cuts'][0]['edit_index'], 1)

    def test_partial_caption_always_rejected(self):
        for boundary in [(1, 3), (3, 5), (2.4, 2.6)]:
            with self.subTest(boundary=boundary), self.assertRaisesRegex(speech.RefineError, 'partially crosses a cut'):
                speech.refine_clip(clip(captions=[caption(2, 4)]), {'cuts': [cut(*boundary)]}, 25)

    def test_replacement_captions_are_complete_old_output_list(self):
        original = clip(captions=[caption(0, 8, '原字幕含口癖需要拆分')])
        edits = {'cuts': [cut(2, 3)], 'captions': [caption(.125, 2, '保留前半句'), caption(3, 7.625, '保留后半句')]}
        result, report = speech.refine_clip(original, edits, 25)
        self.assertTrue(report['captions']['replacement_used'])
        self.assertEqual(result['captions'], [caption(.125, 2, '保留前半句'), caption(2, 6.625, '保留后半句')])
        self.assertEqual(result['caption_timebase'], 'output')
        self.assertEqual(original['captions'][0]['end'], 8)

    def test_contiguous_keep_caption_joins_but_partial_reuse_rejected(self):
        original = clip(keep=[[0, 2], [2, 4]], caption_timebase='window', captions=[caption(1, 3)])
        result, _ = speech.refine_clip(original, {'cuts': [cut(0, 1)]}, 25)
        self.assertEqual(result['captions'], [caption(0, 2)])
        original.update(keep=[[4, 6], [0, 8]], captions=[caption(3, 7)])
        with self.assertRaisesRegex(speech.RefineError, 'repeated/trimmed occurrence'):
            speech.refine_clip(original, {'cuts': []}, 25)

    def test_deleted_window_gap_is_not_silently_dropped(self):
        for words in [caption(1, 3.5), caption(2.2, 2.8)]:
            with self.subTest(words=words), self.assertRaisesRegex(speech.RefineError, 'original window caption'):
                speech.refine_clip(clip(keep=[[0, 2], [3, 4]], caption_timebase='window', captions=[words]), {'cuts': []}, 25)

    def test_broll_source_advances_freeze_does_not_and_labels_map(self):
        original = clip(keep=[[0, 16]],
                        broll=[{'at': 2, 'duration': 6, 'source_start': 500, 'crop': [0, 0, 1000, 800], 'note': '真实演示'},
                               {'at': 8, 'duration': 6, 'source_start': 700, 'freeze': True}],
                        labels=[{'output_start': 1, 'output_end': 15, 'text': '作者与工具'}],
                        opening_hook={'text': '开头结果', 'duration': 6}, cover_output_time=4)
        result, report = speech.refine_clip(original, {'cuts': [cut(4, 6), cut(10, 12)]}, 25)
        self.assertEqual([(b['at'], b['duration'], b['source_start']) for b in result['broll']],
                         [(2, 2, 500), (4, 2, 504), (6, 2, 700), (8, 2, 700)])
        self.assertEqual(result['broll'][1]['note'], '真实演示')
        self.assertEqual(result['broll'][1]['crop'], [0, 0, 1000, 800])
        self.assertEqual([(l['output_start'], l['output_end']) for l in result['labels']], [(1, 4), (4, 8), (8, 11)])
        self.assertEqual(result['cover_output_time'], 4)
        self.assertEqual(report['cover']['selected_before_time'], 6)
        self.assertEqual(report['cover']['selection'], 'next_retained_frame')
        self.assertEqual(result['opening_hook']['duration'], 4)

    def test_fully_removed_overlay_hook_and_tail_cover(self):
        original = clip(broll=[{'at': 0, 'duration': 2, 'source_start': 500}],
                        labels=[{'output_start': 0, 'output_end': 2, 'text': '片头'}],
                        opening_hook={'text': '展示结果', 'duration': 2}, cover_output_time=9)
        result, report = speech.refine_clip(original, {'cuts': [cut(0, 2), cut(8, 10)]}, 25)
        self.assertNotIn('opening_hook', result)
        self.assertEqual(result['broll'], [])
        self.assertEqual(result['labels'], [])
        self.assertEqual(result['cover_output_time'], 5.96)
        self.assertEqual(report['cover']['selected_before_time'], 7.96)
        self.assertTrue(report['opening_hook']['removed'])
        self.assertTrue(any('Redesign' in r for r in report['manual_review']))
        self.assertEqual([p['position'] for p in report['new_cut_points']], ['start', 'end'])

    def test_cover_at_cut_end_is_retained_and_hook_clamped(self):
        original = clip(keep=[[0, 4]], cover_output_time=2, opening_hook={'text': '标题', 'duration': 9})
        result, report = speech.refine_clip(original, {'cuts': [cut(0, 2)]}, 25)
        self.assertEqual(result['cover_output_time'], 0)
        self.assertEqual(report['cover']['selection'], 'retained')
        self.assertEqual(result['opening_hook']['duration'], 2)

    def test_invalid_and_full_deletions(self):
        bad = [[cut(-1, 1)], [cut(1, 1)], [cut(0, 11)], [cut(0, 10)], [cut(.01, 1)],
               [cut(0, 3), cut(2, 4)], [cut(float('nan'), 2)], [cut(1, float('inf'))],
               [cut(1, 2, kind='keyword')], [cut(1, 2, reason=' ')], [cut(True, 2)],
               [cut(0, 5), cut(5, 10)]]
        for cuts in bad:
            with self.subTest(cuts=cuts), self.assertRaises(speech.RefineError):
                speech.refine_clip(clip(), {'cuts': cuts}, 25)

    def test_waiting_can_have_empty_text_and_non25_frame_grid(self):
        original = clip(keep=[[0, 1]], cover_output_time=0)
        result, _ = speech.refine_clip(original, {'cuts': [cut(0, 1 / 30, kind='waiting', words='')]}, 30)
        self.assertAlmostEqual(result['duration'], 29 / 30)
        with self.assertRaises(speech.RefineError):
            speech.refine_clip(original, {'cuts': [cut(0, 1 / 30, kind='waiting', words='')]}, 25)


class PathAndCLITests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get('SPEECH_TEST_TMP')
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='refine-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "original project ' 中文"
        self.old.mkdir()
        for filename in ('source.mp4', 'font.ttc', 'logo.png'):
            (self.old / filename).write_bytes(b'input fixture; media is never read')
        (self.old / 'output').mkdir()
        (self.old / 'work').mkdir()
        (self.old / 'output' / 'old.mp4').write_bytes(b'old output')
        self.project = {'schema_version': 1, 'source': 'source.mp4', 'output_dir': 'output', 'work_dir': 'work',
                        'event': {'name': '共学', 'date': '2026.01.01', 'footer': '一起实践'},
                        'style': {'font': 'font.ttc', 'logo': 'logo.png'}, 'clips': [clip()]}
        self.project_file = self.old / 'project.json'
        self.edits_file = self.base / 'edits.json'
        self.out = self.base / 'cleaned' / 'project.json'
        self.write_inputs()

    def write_inputs(self, edits=None):
        self.project_file.write_text(json.dumps(self.project, ensure_ascii=False), encoding='utf-8')
        self.edits_file.write_text(json.dumps(edits or {'schema_version': 1, 'timebase': 'output',
                                                      'clips': [{'id': '01', 'cuts': [cut(2, 3)]}]}), encoding='utf-8')

    def test_relocates_paths_report_and_input_unchanged(self):
        before = {p: p.read_bytes() for p in self.old.rglob('*') if p.is_file()}
        edits_before = self.edits_file.read_bytes()
        report = speech.refine(self.project_file, self.edits_file, self.out, 'new media', 'render work')
        cleaned = json.loads(self.out.read_text())
        self.assertEqual(cleaned['source'], str(self.old / 'source.mp4'))
        self.assertEqual(cleaned['style']['font'], str(self.old / 'font.ttc'))
        self.assertEqual(cleaned['style']['logo'], str(self.old / 'logo.png'))
        self.assertEqual(cleaned['output_dir'], str(self.out.parent / 'new media'))
        self.assertEqual(cleaned['work_dir'], str(self.out.parent / 'render work'))
        self.assertFalse(Path(cleaned['output_dir']).exists())
        self.assertFalse(Path(cleaned['work_dir']).exists())
        self.assertEqual(json.loads((self.out.parent / speech.REPORT_NAME).read_text()), report)
        self.assertEqual(before, {p: p.read_bytes() for p in self.old.rglob('*') if p.is_file()})
        self.assertEqual(edits_before, self.edits_file.read_bytes())

    def test_default_logo_is_repo_relative_and_unspecified_font_stays_auto(self):
        self.project['style'] = {}
        self.write_inputs()
        speech.refine(self.project_file, self.edits_file, self.out)
        style = json.loads(self.out.read_text())['style']
        self.assertEqual(style['logo'], str(ROOT / 'assets' / 'brand-logo-white.png'))
        self.assertNotIn('font', style)

    def test_cli_from_unrelated_cwd(self):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'refine_speech.py'), '--project',
                              str(self.project_file), '--edits', str(self.edits_file), '--out', str(self.out)],
                             cwd=self.base, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['deleted_duration'], 1)
        self.assertEqual(json.loads(self.out.read_text())['clips'][0]['duration'], 9)

    def test_refuses_existing_input_project_report_and_output(self):
        for target in [self.project_file, self.edits_file, self.old / 'source.mp4', self.old / 'output' / 'new.json']:
            with self.subTest(target=target), self.assertRaises(speech.RefineError):
                speech.refine(self.project_file, self.edits_file, target)
        self.out.parent.mkdir()
        for occupied in [self.out, self.out.parent / speech.REPORT_NAME]:
            occupied.write_text('do not overwrite')
            with self.subTest(occupied=occupied), self.assertRaises(speech.RefineError):
                speech.refine(self.project_file, self.edits_file, self.out)
            self.assertEqual(occupied.read_text(), 'do not overwrite')
            occupied.unlink()

    def test_refuses_reused_nested_and_ancestor_output_work_paths(self):
        for output, work in [(self.old / 'output', None), (self.old / 'work' / 'nested', None),
                             (self.old, None), (None, self.old / 'output'),
                             ('same', 'same/nested'), ('same', 'same'), (self.out.parent, None)]:
            with self.subTest(output=output, work=work), self.assertRaises(speech.RefineError):
                speech.refine(self.project_file, self.edits_file, self.out,
                              str(output) if output else None, str(work) if work else None)
        self.assertFalse(self.out.parent.exists())

    def test_symlink_alias_cannot_reuse_old_paths(self):
        alias = self.base / 'old-output-alias'
        alias.symlink_to(self.old / 'output', target_is_directory=True)
        with self.assertRaises(speech.RefineError):
            speech.refine(self.project_file, self.edits_file, self.out, str(alias))
        alias.unlink()
        alias.symlink_to(self.project_file)
        with self.assertRaises(speech.RefineError):
            speech.refine(self.project_file, self.edits_file, alias)

    def test_dangling_output_symlink_is_rejected_before_resolving(self):
        alias = self.base / 'requested-project.json'
        missing_target = self.base / 'must-not-be-created' / 'project.json'
        alias.symlink_to(missing_target)
        original_project = self.project_file.read_bytes()
        original_edits = self.edits_file.read_bytes()
        self.assertTrue(alias.is_symlink())
        self.assertFalse(alias.exists())
        with self.assertRaisesRegex(speech.RefineError, 'Refusing to overwrite existing destination'):
            speech.refine(self.project_file, self.edits_file, alias)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.readlink(), missing_target)
        self.assertFalse(missing_target.parent.exists())
        self.assertFalse((self.base / speech.REPORT_NAME).exists())
        self.assertEqual(self.project_file.read_bytes(), original_project)
        self.assertEqual(self.edits_file.read_bytes(), original_edits)

    def test_invalid_cut_creates_no_destination_and_keeps_inputs(self):
        self.project['clips'][0]['captions'] = [caption(0, 5)]
        self.write_inputs()
        before = self.project_file.read_bytes()
        with self.assertRaisesRegex(speech.RefineError, 'partially crosses a cut'):
            speech.refine(self.project_file, self.edits_file, self.out)
        self.assertFalse(self.out.parent.exists())
        self.assertEqual(before, self.project_file.read_bytes())

    def test_unknown_duplicate_ids_and_wrong_timebase_rejected(self):
        examples = [
            {'schema_version': 1, 'timebase': 'window', 'clips': [{'id': '01', 'cuts': []}]},
            {'schema_version': 1, 'timebase': 'output', 'clips': [{'id': '99', 'cuts': []}]},
            {'schema_version': 1, 'timebase': 'output', 'clips': [{'id': '01', 'cuts': []}, {'id': '01', 'cuts': []}]},
            {'schema_version': 1, 'timebase': 'output', 'clips': [{'id': '01'}]},
        ]
        for edits in examples:
            self.write_inputs(edits)
            with self.subTest(edits=edits), self.assertRaises(speech.RefineError):
                speech.refine(self.project_file, self.edits_file, self.out)
        self.assertFalse(self.out.parent.exists())

    def test_unedited_clips_are_retained_and_have_mapped_captions(self):
        self.project['clips'].append(clip(id='02', keep=[[2, 6]], caption_timebase='window', captions=[caption(2, 4)]))
        self.write_inputs()
        report = speech.refine(self.project_file, self.edits_file, self.out)
        result = json.loads(self.out.read_text())
        self.assertEqual(result['clips'][1]['keep'], [[2, 6]])
        self.assertEqual(result['clips'][1]['captions'], [caption(0, 2)])
        self.assertEqual(report['clips'][1]['deleted_duration'], 0)


if __name__ == '__main__':
    unittest.main()
