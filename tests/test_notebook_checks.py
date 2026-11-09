"""Offline checks for the Part 1 notebooks (task1/lab/task/part1) and the Home notebook, and the one rule that
the coding agent's lab context file (docs/_static/vss-lab-context.txt) shares with notebook 3: RT-VLM's frames.

Run from the repo root or inside the lab:  python3 -I task1/tests/test_notebook_checks.py
NOTEBOOK_ROOT overrides the part1 directory. The default is /dli/task/part1 when it exists (the lab
image, and CI, which symlinks /dli/task), otherwise task1/lab/task/part1 next to this file.

The platform-touching cells are exercised by id with every I/O mocked (requests, docker, the clock,
the status files), so nothing here needs the stack, and no executed outputs are ever stored. Needs
`requests`; `nbformat` is used for schema validation when it is importable (lab image and CI).
"""
import builtins
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import requests

try:
    import nbformat
except ImportError:  # a bare interpreter; the lab image and CI carry it
    nbformat = None

_LAB_ROOT = Path('/dli/task/part1')
ROOT = Path(os.environ.get('NOTEBOOK_ROOT') or (
    _LAB_ROOT if _LAB_ROOT.is_dir() else Path(__file__).resolve().parents[1] / 'lab/task/part1'))
TASK = ROOT.parent                      # /dli/task: course_path.json paths are relative to it
HOME = TASK / '00_Home.ipynb'
PATH_FILE = ROOT / 'course_path.json'
PART1_NOTEBOOKS = sorted(ROOT.glob('*.ipynb'))

NB00 = '00-onboarding.ipynb'
NB01 = '01-inspect-vss-deployment.ipynb'
NB02 = '02-rt-cv-detection-and-tracking.ipynb'
NB03 = '03-rt-vlm-captioning-and-prompting.ipynb'
NB04 = '04-rt-embed-video-search.ipynb'
ASSESSMENT = 'assessment.ipynb'
# The six Part 1 notebooks and the title line each one opens with (after "# "). The last step of Part 1,
# VSS Coding Agent Skills, is a Course Docs page since 2026-11-04 (docs/course/vss-coding-agent-skills.md),
# not a notebook; its notebook was deleted.
TITLES = {
    NB00: '0. Course Onboarding',
    NB01: '1. Inspect VSS Deployment',
    NB02: '2. RT-CV: Detection & Tracking',
    NB03: '3. RT-VLM: Captioning & Prompting',
    NB04: '4. RT-Embed: Video Search',
    ASSESSMENT: 'Part 1 Assessment',
}
RETIRED_AGENT_NOTEBOOK = 'coding-agent-' + 'skills.ipynb'
# "Your turn" exercises per notebook. The building-block notebooks pair each one with a hidden solution;
# the assessment has one per task (p1-t1 to p1-t7) and no solutions. Onboarding and notebook 1 have none.
EXERCISES = {NB00: 0, NB01: 0, NB02: 4, NB03: 2, NB04: 2, ASSESSMENT: 7}
# Every figure a Part 1 notebook or Home may show (images/course/<id>.png or .gif).
FIGURES = {'day-map', 'where-things-run', 'data-path', 'gpu-map', 'data-gap', 'map-ingest', 'ingest-handshake',
           'three-names', 'nested-docs', 'boxes-to-tracklet', 'map-caption', 'chunking', 'prompt-anatomy',
           'caption-cost', 'map-search', 'embedding-knn', 'two-anchors'}
SCHEMA_PB2 = Path('/opt/vss/tools/message-broker-consumers/schema_pb2.py')
SEED_STATUS = '/opt/vss/_course-runtime/seed-status'
HEADER_IMG = 'images/nvidia_header.png'
# The eleven containers that o0-gate, a3d3cf2d, deploy/wait-for-vss.sh and the lab pill
# (lab_home.LAB_CHECKS) all require. Keep the four in step.
CONTAINERS = ['vss-agent', 'vss-rtvi-cv', 'vss-rtvi-vlm', 'vss-rtvi-embed',
              'vss-vios-ingress', 'vss-vios-sensor', 'vss-vios-streamprocessing',
              'elasticsearch', 'kafka', 'logstash', 'vss-behavior-analytics']
BERLIN_SCENARIOS = ('ready', 'timeout', 'http502', 'red', 'missing',
                    'starting', 'unhealthy', 'stopped', 'docker-failure')
# A code cell is an exercise when its first line is exactly "# TODO" and it carries a bare FIXME token
# (not the string "FIXME" that check cells use as a sentinel).
BARE_FIXME = re.compile(r'''(?<![\w"'])FIXME(?![\w"'])''')
EXERCISE_PROSE = ('**Your turn:**', '**TODO:**')
# Terms and codes of the course before the 2026-11-02 content review; the restructured course does not use them.
OLD_STORY_WORDS = re.compile(
    r"review row|evidence join|hand-?off|verdict rule|\babstain|bounded change|warehouse|review agent|review_agent"
    r"|notebook [67]\b|whole task|the day's question|\b0[AB]\.", re.IGNORECASE)
OLD_STORY_CODES = re.compile(r'\b(?:O[1-6]|A[123]|B(?:10|[0-9]))\b')   # objective, assessment and block codes
# Assembled so that the literals never appear in this file.
RETIRED_NAMES = ('Cosmos ' + 'Reason', 'Cosmos 3 ' + 'Reasoner')
VERIFY_MARKER = 'VERIFY ' + 'ON DEVBOX'
# RT-VLM samples one frame per request unless the request names these fields (devbox run of 2026-11-04). Notebook 3
# asks for 8 in every chat/completions request, and the lab context asks the coding agent for the same.
FRAME_FIELDS = {'num_frames_per_second_or_fixed_frames_chunk': 8, 'use_fps_for_chunking': False}
# The repository copy; a lab has only the published one, so the test that reads it skips there. `.parent` stops at
# the root, so this also works when the file is copied to /tmp in the lab (CS-11), where `parents[2]` would raise.
LAB_CONTEXT = Path(__file__).resolve().parent.parent.parent / 'docs/_static/vss-lab-context.txt'


def notebook(path):
    return json.loads(Path(path).read_text())


def cell(filename, cell_id):
    return ''.join(next(c for c in notebook(ROOT / filename)['cells'] if c['id'] == cell_id)['source'])


def source(item):
    return ''.join(item['source'])


def first_line(item):
    lines = source(item).strip().splitlines()
    return lines[0].strip() if lines else ''


def is_hidden(item):
    return bool(item.get('metadata', {}).get('jupyter', {}).get('source_hidden'))


def is_generated(item):
    """Cells that task1/scripts/course_path.py writes (path header, card, Home map)."""
    return 'course_path' in item.get('metadata', {})


def without_magics(code):
    """The cell's Python: IPython shell (!) and magic (%) lines dropped."""
    return '\n'.join(line for line in code.splitlines() if not line.lstrip().startswith(('!', '%')))


def headings(text):
    """Markdown heading lines outside fenced blocks (a pasted prompt may hold its own '#' lines)."""
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
        elif not fenced and line.startswith('#'):
            yield line


def all_notebooks():
    return PART1_NOTEBOOKS + [HOME]


class FakeClock:
    """Deterministic time.monotonic / time.sleep for the loop cells."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@contextlib.contextmanager
def fake_course_setup(status_tail_text):
    """Inject a stub course_setup so `from course_setup import status_tail` needs no /opt/vss."""
    module = types.ModuleType('course_setup')
    module.status_tail = lambda n=5: status_tail_text
    saved = sys.modules.get('course_setup')
    sys.modules['course_setup'] = module
    try:
        yield module
    finally:
        if saved is None:
            sys.modules.pop('course_setup', None)
        else:
            sys.modules['course_setup'] = saved


def readiness_mocks(scenario):
    """The requests.get response and docker inspect result for one Berlin scenario."""
    rows = [f'/{name}|running|healthy' for name in CONTAINERS]
    if scenario == 'missing':
        rows.pop()
    if scenario in ('starting', 'unhealthy'):
        rows[0] = f'/{CONTAINERS[0]}|running|{scenario}'
    if scenario == 'stopped':
        rows[0] = f'/{CONTAINERS[0]}|exited|healthy'
    response = Mock(status_code=502 if scenario == 'http502' else 200)
    response.json.return_value = {'status': 'red' if scenario == 'red' else 'yellow'}
    result = subprocess.CompletedProcess([], 0, '\n'.join(rows), '')
    return response, result


class ReadinessCellTests(unittest.TestCase):
    """Notebook 1 cell a3d3cf2d: one pass, never a loop (the wait for a cold lab lives in onboarding's o0-gate)."""

    def test_readiness_outcomes(self):
        code = cell(NB01, 'a3d3cf2d')
        for scenario in BERLIN_SCENARIOS:
            with self.subTest(scenario=scenario):
                response, result = readiness_mocks(scenario)
                output, ns = io.StringIO(), {'requests': requests, 'BASE': 'http://test'}
                with patch.object(requests, 'get', return_value=response) as get, \
                     patch.object(subprocess, 'run', return_value=result) as run, \
                     patch.object(time, 'sleep', side_effect=AssertionError('the single pass must not sleep')), \
                     contextlib.redirect_stdout(output):
                    if scenario == 'timeout':
                        get.side_effect = requests.Timeout()
                    if scenario == 'docker-failure':
                        run.side_effect = OSError('unavailable')
                    exec(code, ns)
                self.assertEqual(ns['lab_ready'], scenario == 'ready')
                self.assertIn('READY TO CONTINUE' if scenario == 'ready' else 'NOT READY', output.getvalue())
                self.assertEqual(ns['REQUIRED'], CONTAINERS)
                self.assertNotIn('Kibana', ns['SERVICES'])

    def test_notebook_1_has_no_waiting_loop(self):
        ids = [c['id'] for c in notebook(ROOT / NB01)['cells']]
        self.assertNotIn('nb01-readiness-loop', ids)


class OnboardingCellTests(unittest.TestCase):
    """Onboarding cells o0-gate (the one cell that waits for a cold lab) and o0-prep-status (course-prep status)."""

    GATE_SCENARIOS = BERLIN_SCENARIOS + ('kibana-down', 'seed-failed', 'seed-running')
    FAILED_SEED = ('2026-10-20T12:00:00Z RUNNING: waiting for readiness (cap 3600s)\n'
                   '2026-10-20T13:00:00Z FAILED: readiness timeout after 3600s; not ready: vss-rtvi-vlm(starting)\n')
    RUNNING_SEED = ('2026-10-20T12:00:00Z RUNNING: pulling the VSS profile images\n'
                    '2026-10-20T12:30:00Z RUNNING: waiting for readiness (cap 3600s)\n')
    TAIL = 'seed-status: 2026-10-20T12:00:00Z RUNNING: pulling the VSS profile images\n[vss-deploy] layer 3/9'

    def _run_gate(self, scenario):
        response, result = readiness_mocks(scenario)
        clock, output, ns, error = FakeClock(), io.StringIO(), {}, None
        real_isfile = os.path.isfile
        seed_text = {'seed-failed': self.FAILED_SEED, 'seed-running': self.RUNNING_SEED}.get(scenario)

        def isfile(path):
            if path == SEED_STATUS:
                return seed_text is not None     # never read the real file of the machine running the test
            return real_isfile(path)

        def by_url(url, **kwargs):
            down = 'kibana' in url if scenario == 'kibana-down' else True
            reply = Mock(status_code=502 if down else 200)
            reply.json.return_value = {'status': 'yellow'}
            return reply

        with fake_course_setup(self.TAIL), \
             patch.object(requests, 'get', return_value=response) as get, \
             patch.object(subprocess, 'run', return_value=result) as run, \
             patch.object(time, 'sleep', clock.sleep), patch.object(time, 'monotonic', clock.monotonic), \
             patch('os.path.isfile', isfile), \
             (patch.object(builtins, 'open', mock_open(read_data=seed_text)) if seed_text else contextlib.nullcontext()), \
             contextlib.redirect_stdout(output):
            if scenario == 'timeout':
                get.side_effect = requests.Timeout()
            if scenario == 'docker-failure':
                run.side_effect = OSError('unavailable')
            if scenario in ('kibana-down', 'seed-failed', 'seed-running'):
                get.side_effect = by_url
            try:
                exec(cell(NB00, 'o0-gate'), ns)
            except Exception as exc:  # noqa: BLE001 - the scenario asserts on it
                error = exc
        return ns, output.getvalue(), error, clock

    def test_onboarding_gate_outcomes(self):
        for scenario in self.GATE_SCENARIOS:
            with self.subTest(scenario=scenario):
                ns, out, err, clock = self._run_gate(scenario)
                self.assertEqual(ns['CONTAINERS'], CONTAINERS)
                if scenario in ('ready', 'kibana-down'):
                    self.assertIsNone(err, err)
                    self.assertTrue(ns['lab_ready'])
                    self.assertIn('READY TO CONTINUE', out)
                    self.assertEqual(clock.slept, [])
                    if scenario == 'kibana-down':
                        self.assertIn('OPTIONAL', out)
                elif scenario == 'seed-failed':
                    self.assertIsInstance(err, RuntimeError)
                    self.assertIn('FAILED', str(err))
                    self.assertEqual(clock.slept, [], 'a FAILED seed must stop the loop before the first sleep')
                    self.assertIn('[vss-deploy] layer 3/9', out, 'the status tail is printed before the raise')
                else:
                    self.assertIsInstance(err, TimeoutError, f'{scenario}: {err!r}\n{out}')
                    self.assertFalse(ns['lab_ready'])
                    self.assertIn('NOT READY', out)
                    self.assertTrue(20 * 60 - 15 <= sum(clock.slept) <= 20 * 60, sum(clock.slept))
                    self.assertEqual(out.count('[vss-deploy] layer 3/9'), 1, 'unchanged status tail printed more than once')
                    if scenario == 'docker-failure':
                        self.assertIn('Docker unavailable', out)
                    else:
                        self.assertEqual(out.count(f' {CONTAINERS[1]:27} '), 1, 'an unchanged probe row was printed more than once')

    def test_onboarding_prep_status(self):
        code = cell(NB00, 'o0-prep-status')
        assets = {'normal_eval.mp4': {'sensor_id': 'uuid-1', 'state': 'ok', 'error': None},
                  'rain_eval.mp4': {'sensor_id': None, 'state': 'pending', 'error': None}}
        cases = {'OK': {'state': 'OK', 'assets': assets, 'updated': '2026-10-20T08:00:00Z'},
                 'RUNNING': {'state': 'RUNNING', 'assets': assets, 'updated': '2026-10-20T08:00:00Z'},
                 'FAILED': {'state': 'FAILED', 'assets': assets, 'updated': '2026-10-20T08:00:00Z'},
                 'missing': None}
        for name, payload in cases.items():
            with self.subTest(state=name):
                output, ns, error = io.StringIO(), {}, None
                with patch('os.path.isfile', lambda p, present=payload is not None: present), \
                     patch.object(builtins, 'open', mock_open(read_data=json.dumps(payload or {}))), \
                     contextlib.redirect_stdout(output):
                    try:
                        exec(code, ns)
                    except Exception as exc:  # noqa: BLE001
                        error = exc
                if name == 'OK':
                    self.assertIsNone(error, error)
                    self.assertIn('PREPARATION OK', output.getvalue())
                    self.assertIn('normal_eval.mp4', output.getvalue())
                else:
                    self.assertIsInstance(error, RuntimeError, f'{name}: {error!r}')
                    self.assertNotIn('PREPARATION OK', output.getvalue())
                    if name == 'RUNNING':
                        self.assertIn('RUNNING', str(error))
                    if name == 'missing':
                        self.assertIn('does not exist', str(error))


class CaptioningFramesTests(unittest.TestCase):
    """Notebook 3 and RT-VLM's sampled frames. On the devbox (2026-11-04) RT-VLM sampled a single frame unless a
    request named the frames fields: every answer of sections 3.5 to 3.7 described the first second of the clip and
    missed the collision. Section 3.4 still teaches the default against 8 frames."""

    @staticmethod
    def _ask():
        """Run cell 04b-ask against a stub OpenAI client; return the cell's namespace and the stub's create()."""
        reply = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='yes'), finish_reason='stop')],
            usage=types.SimpleNamespace(prompt_tokens=19, completion_tokens=1))
        create = Mock(return_value=reply)
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        ns = {'client': client, 'MODEL': 'cosmos-test', 'video_data_url': 'data:video/mp4;base64,AAAA', 'time': time}
        exec(cell(NB03, '04b-ask'), ns)
        return ns, create

    def test_ask_sends_the_frames_fields_on_every_request(self):
        ns, create = self._ask()
        self.assertEqual(ns['FRAMES'], FRAME_FIELDS['num_frames_per_second_or_fixed_frames_chunk'])
        result = ns['ask']('Is there a collision?', max_tokens=10, temperature=0.2, top_p=0.2)
        self.assertEqual((result['text'], result['prompt_tokens'], result['completion_tokens']), ('yes', 19, 1))
        sent = create.call_args.kwargs
        self.assertEqual(sent['extra_body'], FRAME_FIELDS)
        self.assertEqual((sent['model'], sent['max_tokens'], sent['temperature'], sent['top_p']),
                         ('cosmos-test', 10, 0.2, 0.2))
        self.assertEqual([part['type'] for part in sent['messages'][1]['content']], ['video_url', 'text'])
        ns['FRAMES'] = 16     # read at call time: a learner who changes FRAMES need not rerun the helper's cell
        ns['ask']('Again.')
        self.assertEqual(create.call_args.kwargs['extra_body'],
                         {**FRAME_FIELDS, 'num_frames_per_second_or_fixed_frames_chunk': 16})

    def test_every_chat_request_goes_through_ask(self):
        """Only ask() calls chat/completions, so no request of sections 3.5 to 3.7 can leave the frames out."""
        callers = [c['id'] for c in notebook(ROOT / NB03)['cells']
                   if c['cell_type'] == 'code' and re.search(r'chat[./]completions', source(c))]
        self.assertEqual(callers, ['04b-ask'])

    def test_section_3_4_still_compares_the_default_with_8_frames(self):
        """Sections 3.2 to 3.4 caption at the deployment default until nb03-frames-8 asks for 8 frames: the contrast
        is the lesson. 3.2 points ahead to it, and 04b-ask-md says why every chat request asks for FRAMES."""
        ids = [c['id'] for c in notebook(ROOT / NB03)['cells']]
        for cell_id in ('cc904416', 'a574d3b9', '9b3f48c7', 'a5afe156', 'nb03-fields-code'):
            with self.subTest(cell=cell_id):
                self.assertIn('course_setup.caption(', cell(NB03, cell_id))
                self.assertNotIn('frames=', cell(NB03, cell_id))
        self.assertIn('frames=8, use_fps=False', cell(NB03, 'nb03-frames-8'))
        self.assertLess(ids.index('nb03-frames-default'), ids.index('nb03-frames-8'))
        self.assertLess(ids.index('nb03-frames-8'), ids.index('04b-ask'))
        pointer = cell(NB03, '7ba26e6e')
        self.assertIn('`frame_count`', pointer)
        self.assertIn('Section 3.4', pointer)
        why = cell(NB03, '04b-ask-md')
        for needle in ('`FRAMES = 8`', 'section 3.4', '`extra_body`', '`prompt_tokens`'):
            self.assertIn(needle, why)

    def test_caption_search_names_the_word_forms(self):
        """Section 3.8: a match query finds whole words. On the devbox "collision" found nothing because the caption
        said "collided", so the cell searches the word alone and then its forms."""
        stored = ['An empty intersection with clear road markings.',
                  'A white pickup truck and a red sedan have collided at an intersection.']
        queries = []

        def es(method, path, **kwargs):
            self.assertEqual((method, path), ('POST', '/default_abc/_search'))
            words = kwargs['json']['query']['match']['text']
            queries.append(words)
            wanted = set(words.lower().split())          # a match query ORs its words and matches whole words only
            found = [text for text in stored if wanted & set(re.findall(r'[a-z]+', text.lower()))]
            return {'hits': {'total': {'value': len(found)}, 'hits': [{'_source': {'text': t}} for t in found]}}

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(cell(NB03, 'd1f22825'), {'course_setup': types.SimpleNamespace(es=es), 'index': 'default_abc'})
        self.assertEqual(queries[0], 'collision')
        self.assertLessEqual({'collision', 'collided', 'crash'}, set(queries[-1].split()))
        self.assertIn("0 matching chunks for 'collision'", output.getvalue())
        self.assertIn('have collided', output.getvalue())
        self.assertIn('latest caption for each chunk', cell(NB03, '30587c82'))

    def test_the_reasoning_question_does_not_assume_the_answers_differ(self):
        """With 8 frames the direct and the reasoning call of section 3.7 may agree; question 4 asks first."""
        question, answers = cell(NB03, '2596dd83').split('<details>', 1)
        self.assertIn('4. Did the direct call and the reasoning call in section 3.7 agree?', question)
        self.assertIn('4. Agreement is not proof', answers)

    @unittest.skipUnless(LAB_CONTEXT.is_file(), 'the lab context file is in the repository (docs/_static), not the lab')
    def test_the_lab_context_asks_rt_vlm_for_the_same_frames(self):
        """The coding agent asks RT-VLM for the frames notebook 3 asks for: in file captions and chat completions
        always, in the live-stream request only where that request takes the fields."""
        bullets = [' '.join(item.split()) for item in LAB_CONTEXT.read_text(encoding='utf-8').split('\n- ')]

        def bullet(start):
            found = [b for b in bullets if b.startswith(start)]
            self.assertEqual(len(found), 1, f'{LAB_CONTEXT.name}: one bullet must start with {start!r}')
            return found[0]

        rule = bullet('RT-VLM frames:')
        named = re.search(r'\{"num_frames_per_second_or_fixed_frames_chunk": (\d+), "use_fps_for_chunking": (\w+)\}',
                          rule)
        self.assertIsNotNone(named, 'the rule names both fields as JSON')
        ns, _ = self._ask()
        self.assertEqual((int(named.group(1)), named.group(2)), (ns['FRAMES'], 'false'))
        for route in ('http://nginx/rtvi-vlm/v1/generate_captions', 'http://nginx/rtvi-vlm/v1/chat/completions'):
            self.assertIn(route, rule)
        self.assertIn('RT-VLM frames', bullet('Captions:'))
        self.assertIn('only if the skill or RT-VLM', bullet('RT-VLM watches the stream.'))


class NotebookChecks(unittest.TestCase):
    """Static checks over every shipped Part 1 notebook (and Home where it applies)."""

    @unittest.skipUnless(SCHEMA_PB2.is_file(), 'schema_pb2 ships only in the lab image (/opt/vss)')
    def test_protobuf_display_is_bounded(self):
        sys.path.insert(0, str(SCHEMA_PB2.parent))
        from schema_pb2 import Frame
        frame = Frame(id='fixture', sensorId='fixture-camera')
        frame.timestamp.FromJsonString('2025-01-01T00:00:00Z')
        for index in range(10):
            obj = frame.objects.add(id=str(index), type='car', confidence=0.9)
            obj.embedding.vector.extend([0.25] * 1151 + [98765.0])
        for sample in (frame, Frame(id='empty', sensorId='fixture-camera')):
            ns, output = {'payload': sample.SerializeToString()}, io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(cell(NB02, 'kafka-decode-code'), ns)
            self.assertNotIn('98765', output.getvalue())
            self.assertLess(len(output.getvalue()), 4000)
            self.assertEqual(output.getvalue().count('"tracklet_id"'), min(5, len(sample.objects)))
            self.assertEqual(ns['frame'].SerializeToString(), sample.SerializeToString())

    def test_the_lab_holds_home_and_the_six_part1_notebooks(self):
        self.assertEqual(sorted(p.name for p in PART1_NOTEBOOKS), sorted(TITLES), f'notebooks under {ROOT}')
        shipped = []
        for folder, dirs, files in os.walk(TASK):
            # data/ (course data in the lab) and work/ (learner output) are not shipped content
            dirs[:] = [d for d in dirs if d not in ('data', 'work', '.ipynb_checkpoints', '__pycache__')]
            shipped += [(Path(folder) / f).relative_to(TASK).as_posix() for f in files if f.endswith('.ipynb')]
        # remix/00_Home.ipynb is the alternate Home page (lab/task/remix/); it ships beside the original.
        self.assertEqual(sorted(shipped), sorted(['00_Home.ipynb', 'remix/00_Home.ipynb'] + [f'part1/{name}' for name in TITLES]))

    def test_nothing_links_the_retired_agent_notebook(self):
        """VSS Coding Agent Skills is a Course Docs page now; no notebook, Home or START_HERE.md still names the
        notebook it replaced, and the path sends learners to Course Docs for it."""
        for path in all_notebooks() + [TASK / 'START_HERE.md']:
            with self.subTest(file=path.name):
                self.assertNotIn(RETIRED_AGENT_NOTEBOOK, path.read_text())
        agent = next(s for s in json.loads(PATH_FILE.read_text())['steps'] if s['id'] == 'agent')
        self.assertNotIn('path', agent)
        self.assertEqual(agent['course_docs'], 'course/vss-coding-agent-skills.md')
        home_map = ''.join(next(c for c in notebook(HOME)['cells'] if c['id'] == 'home-map')['source'])
        self.assertIn('| VSS Coding Agent Skills (open Course Docs from the JupyterLab Launcher) |', home_map)

    def test_notebooks_are_valid_and_unexecuted(self):
        self.assertTrue(PART1_NOTEBOOKS, f'no notebooks under {ROOT}; set NOTEBOOK_ROOT')
        for path in all_notebooks():
            with self.subTest(notebook=path.name):
                text = path.read_text()
                nb = json.loads(text)
                self.assertEqual((nb['nbformat'], nb['nbformat_minor']), (4, 5), path.name)
                if nbformat is not None:
                    nbformat.validate(nbformat.reads(text, as_version=4))
                self.assertEqual(nb['metadata'].get('kernelspec', {}).get('name'), 'python3', f'{path.name}: kernelspec')
                ids = [item.get('id') for item in nb['cells']]
                self.assertEqual(len(ids), len(set(ids)), f'{path.name}: duplicate cell ids')
                for cell_id in ids:
                    self.assertRegex(cell_id or '', r'^[A-Za-z0-9_-]{1,64}$', f'{path.name}: bad cell id {cell_id!r}')
                for item in nb['cells']:
                    if item['cell_type'] == 'code':
                        self.assertFalse(item.get('outputs'), f'{path.name} {item["id"]}: outputs not stripped')
                        self.assertIsNone(item.get('execution_count'), f'{path.name} {item["id"]}: execution count left')
                        compile(without_magics(source(item)), f'{path.name}:{item["id"]}', 'exec')

    def test_house_pattern_structure(self):
        """Header image first and last; at cells[1] the title, two or three sentences, "In this notebook you will:"
        with three to five bullets and "**Time:**"; a "## Next" cell; "## Check your understanding" except in the
        assessment. course_path.py puts its path header right after cells[1]."""
        for path in PART1_NOTEBOOKS:
            with self.subTest(notebook=path.name):
                cells = notebook(path)['cells']
                markdown = [source(c) for c in cells if c['cell_type'] == 'markdown']
                self.assertEqual(cells[0]['cell_type'], 'markdown')
                self.assertIn(HEADER_IMG, source(cells[0]), 'header image must be the first cell')
                self.assertEqual(cells[-1]['cell_type'], 'markdown')
                self.assertIn(HEADER_IMG, source(cells[-1]), 'header image must be the last cell')
                title = source(cells[1])
                self.assertEqual(cells[1]['cell_type'], 'markdown')
                self.assertEqual(first_line(cells[1]), '# ' + TITLES.get(path.name, '?'), 'title cell at cells[1]')
                self.assertIn('In this notebook you will:', title)
                bullets = []
                for line in title.split('In this notebook you will:', 1)[1].splitlines():
                    if line.startswith('**Time:**'):
                        break
                    if line.startswith('- '):
                        bullets.append(line)
                self.assertTrue(3 <= len(bullets) <= 5, f'{len(bullets)} bullets after "In this notebook you will:"')
                self.assertIn('**Time:**', title)
                self.assertTrue(any(re.match(r'## Next\b', m) for m in markdown), 'a "## Next" bridge cell')
                if path.name != ASSESSMENT:
                    self.assertTrue(any('## Check your understanding' in m for m in markdown))

    def test_titles_match_the_course_path(self):
        """Each step's title in part1/course_path.json is the title its notebook opens with."""
        steps = [s for s in json.loads(PATH_FILE.read_text())['steps'] if s.get('path')]
        self.assertEqual(sorted(s['path'] for s in steps), sorted(f'part1/{name}' for name in TITLES))
        for step in steps:
            with self.subTest(step=step['id']):
                self.assertEqual(first_line(notebook(TASK / step['path'])['cells'][1]), '# ' + step['title'])

    def test_sections_are_numbered_after_the_title(self):
        """A notebook titled "# N. ..." is part1/0N-*.ipynb and numbers its sections "## N.1", "## N.2", ... in
        order. Its other "##" headings close the notebook (check, next, instructor only)."""
        closing = ('## Check your understanding', '## Next', '## Instructor only')
        numbered = 0
        for path in PART1_NOTEBOOKS:
            cells = notebook(path)['cells']
            title = re.match(r'# (\d+)\. ', first_line(cells[1]))
            if not title:
                continue
            numbered += 1
            n = int(title.group(1))
            with self.subTest(notebook=path.name):
                self.assertTrue(path.name.startswith(f'{n:02d}-'), f'"# {n}." in {path.name}')
                sections = []
                for item in cells:
                    if item['cell_type'] != 'markdown' or is_generated(item):
                        continue
                    for line in headings(source(item)):
                        if not line.startswith('## '):
                            continue
                        found = re.match(rf'## {n}\.(\d+) \S', line)
                        if found:
                            sections.append(int(found.group(1)))
                        else:
                            self.assertTrue(line.startswith(closing), f'{item["id"]}: {line!r} is not "## {n}.k ..."')
                self.assertEqual(sections, list(range(1, len(sections) + 1)), 'sections out of order or skipped')
                self.assertTrue(sections, 'no numbered sections')
        self.assertEqual(numbered, 5, 'onboarding and notebooks 1 to 4 carry numbered titles')

    def test_section_references_resolve(self):
        """Every "section N.k" in a Part 1 notebook or Home names a "## N.k" heading that exists."""
        existing = set()
        for path in PART1_NOTEBOOKS:
            for item in notebook(path)['cells']:
                if item['cell_type'] == 'markdown':
                    for line in headings(source(item)):
                        found = re.match(r'## (\d+)\.(\d+) ', line)
                        if found:
                            existing.add((int(found.group(1)), int(found.group(2))))
        self.assertTrue(existing)
        reference = re.compile(r'\bsections? (\d+)\.(\d+)(?:\s*(?:,|and|or|to)\s*(\d+)\.(\d+))?', re.IGNORECASE)
        for path in all_notebooks():
            for item in notebook(path)['cells']:
                for match in reference.finditer(source(item)):
                    for major, minor in (match.group(1, 2), match.group(3, 4)):
                        if major is None:
                            continue
                        with self.subTest(notebook=path.name, cell=item['id'], reference=match.group(0)):
                            self.assertIn((int(major), int(minor)), existing, f'no "## {major}.{minor}" heading')

    def test_milestones_are_on_the_core_path(self):
        """The cell that marks a step done (milestone_cell_id in course_path.json) is a code cell in the step's
        notebook, outside every section or subsection whose heading says Optional."""
        for step in json.loads(PATH_FILE.read_text())['steps']:
            milestone = step.get('milestone_cell_id')
            if not milestone:
                continue
            with self.subTest(step=step['id']):
                cells = notebook(TASK / step['path'])['cells']
                ids = [c['id'] for c in cells]
                self.assertIn(milestone, ids)
                index = ids.index(milestone)
                self.assertEqual(cells[index]['cell_type'], 'code')
                section = subsection = ''
                for item in cells[:index]:
                    if item['cell_type'] != 'markdown':
                        continue
                    for line in headings(source(item)):
                        if line.startswith('## '):
                            section, subsection = line, ''
                        elif line.startswith('### '):
                            subsection = line
                self.assertNotIn('optional', (section + subsection).lower(), f'{milestone} sits under {section!r} {subsection!r}')

    def test_02_optional_section_comes_last(self):
        """Section 2.13 is optional: it follows the milestone and section 2.12, holds the trajectory plot and the
        live stream (with its release cell), ends before the check, and its code still compiles."""
        cells = notebook(ROOT / NB02)['cells']
        ids = [c['id'] for c in cells]
        start, end = ids.index('nb02-optional-md'), ids.index('547badc4')
        self.assertTrue(source(cells[start]).startswith('## 2.13 Optional:'), first_line(cells[start]))
        self.assertTrue(source(cells[end]).startswith('## Check your understanding'), first_line(cells[end]))
        self.assertLess(ids.index('03-ex3-check'), start)
        self.assertLess(ids.index('nb02-appearance-md'), start)
        for cell_id in ('80336c7b', '18cc428b', 'c8a7153c', '770b8dba', '4b49547e', '0926cc76',
                        'nb02-live-index-wait', '22421e4c', 'nb02-release-code'):
            self.assertTrue(start < ids.index(cell_id) < end, f'{cell_id} is outside section 2.13')
        for item in cells[start:end]:
            if item['cell_type'] == 'code':
                compile(without_magics(source(item)), f'{NB02}:{item["id"]}', 'exec')

    def test_exercise_shape(self):
        """A "**Your turn:**" (or "**TODO:**") markdown cell, then a code cell whose first line is "# TODO" with
        bare FIXMEs, then a hidden "# SOLUTION" cell without one, then a code cell that checks the result.
        In the assessment the check comes right after the "# TODO" cell, calls quiz.task, and no solution ships."""
        counts = {}
        for path in PART1_NOTEBOOKS:
            cells = notebook(path)['cells']
            if path.name == ASSESSMENT:
                solutions = [c['id'] for c in cells if c['cell_type'] == 'code' and first_line(c).startswith('# SOLUTION')]
                self.assertEqual(solutions, [], 'the assessment ships no solution cells')
            for index, item in enumerate(cells):
                if item['cell_type'] != 'code' or first_line(item) != '# TODO':
                    continue
                with self.subTest(notebook=path.name, cell=item['id']):
                    counts[path.name] = counts.get(path.name, 0) + 1
                    self.assertTrue(BARE_FIXME.search(source(item)), 'a "# TODO" cell without a FIXME to replace')
                    self.assertGreater(index, 0)
                    prose = cells[index - 1]
                    self.assertEqual(prose['cell_type'], 'markdown', 'the instruction must come right before')
                    self.assertTrue(source(prose).lstrip().startswith(EXERCISE_PROSE),
                                    f'{prose["id"]}: does not start with "**Your turn:**"')
                    after = cells[index + 1:index + 3]
                    if path.name == ASSESSMENT:
                        self.assertTrue(after and after[0]['cell_type'] == 'code', 'the check must follow')
                        self.assertIn('quiz.task(', source(after[0]), f'{after[0]["id"]}: the check calls quiz.task')
                        continue
                    self.assertEqual(len(after), 2, 'the solution and the check must follow')
                    solution, check = after
                    self.assertEqual(solution['cell_type'], 'code', 'the solution must be the next cell')
                    self.assertTrue(first_line(solution).startswith('# SOLUTION'), f'{solution["id"]}: first line is not # SOLUTION')
                    self.assertTrue(is_hidden(solution), f'{solution["id"]}: metadata.jupyter.source_hidden missing')
                    self.assertIsNone(BARE_FIXME.search(source(solution)), f'{solution["id"]}: FIXME left in the solution')
                    self.assertEqual(check['cell_type'], 'code', f'{check["id"]}: a check cell must follow the solution')
                    self.assertFalse(first_line(check).startswith(('# TODO', '# SOLUTION')), check['id'])
        for name, expected in EXERCISES.items():
            self.assertEqual(counts.get(name, 0), expected, f'{name}: exercise count changed; update EXERCISES if intended')

    def test_figures_exist_at_full_width_with_alt_text(self):
        """Every <img> points at a shipped file; course figures are listed in FIGURES, 960 px wide, with alt text."""
        for path in all_notebooks():
            for item in notebook(path)['cells']:
                for tag in re.findall(r'<img\b[^>]*>', source(item)):
                    attrs = dict(re.findall(r'\b(src|width|alt)="([^"]*)"', tag))
                    with self.subTest(notebook=path.name, cell=item['id'], src=attrs.get('src')):
                        self.assertIn('src', attrs)
                        self.assertTrue((path.parent / attrs['src']).is_file(), 'image file missing')
                        if 'images/course/' not in attrs['src']:
                            continue
                        self.assertIn(Path(attrs['src']).stem, FIGURES, 'not a Part 1 figure')
                        self.assertEqual(attrs.get('width'), '960')
                        self.assertGreaterEqual(len(attrs.get('alt', '')), 20, 'alt text describes the figure')

    def test_the_old_course_story_is_gone(self):
        """No Part 1 notebook or Home cell uses the terms or codes of the course before the 2026-11-02 review."""
        for path in all_notebooks():
            for item in notebook(path)['cells']:
                text = source(item)
                with self.subTest(notebook=path.name, cell=item['id']):
                    for pattern in (OLD_STORY_WORDS, OLD_STORY_CODES):
                        hit = pattern.search(text)
                        self.assertIsNone(hit, hit and repr(text[max(0, hit.start() - 40):hit.end() + 40]))

    def test_elasticsearch_source_filters_take_the_file_stem(self):
        """CONTRACTS.md section 1: mdx-raw and mdx-behavior are keyed on the file stem; the VIOS name (stem_1) matches nothing."""
        vios_name_vars = r'(SENSOR_NAME|VIOS_NAME|name_in_vios|vios_name\b|\w+\[["\']name["\']\])'
        raw_by_name = re.compile(r'"sensorId\.keyword":\s*' + vios_name_vars)
        raw_by_uuid = re.compile(r'"sensorId\.keyword":\s*(SENSOR_ID|sensor_id)\b')
        behavior_by_name = re.compile(r'"sensor\.id\.keyword":\s*' + vios_name_vars)
        for path in PART1_NOTEBOOKS:
            for item in notebook(path)['cells']:
                if item['cell_type'] != 'code':
                    continue
                code = source(item)
                with self.subTest(file=path.name, cell=item['id']):
                    self.assertIsNone(raw_by_name.search(code), 'sensorId.keyword filtered by the VIOS name')
                    self.assertIsNone(raw_by_uuid.search(code), 'sensorId.keyword filtered by the VIOS UUID')
                    self.assertIsNone(behavior_by_name.search(code), 'sensor.id.keyword filtered by the VIOS name')

    def test_no_forbidden_strings(self):
        """Hard rules: no keys, no retired model names, no prepared-output switch, no silent except; and no
        devbox-verification marker in a notebook (those rows live in docs/DEVBOX_VERIFY.md)."""
        bare_except = re.compile(r'(?m)^\s*except\s*:\s*$')
        silent_except = re.compile(r'except[^\n]*:\s*\n\s*pass\b')
        files = all_notebooks() + sorted(ROOT.glob('*.py'))
        for path in files:
            with self.subTest(file=path.name):
                text = path.read_text()
                for needle in ('nv' + 'api-', 'USE_LIVE') + RETIRED_NAMES:   # key prefix assembled so the literal never ships
                    self.assertNotIn(needle, text)
                if path.suffix == '.ipynb':
                    self.assertNotIn(VERIFY_MARKER, text)
                    code = '\n'.join(source(c) for c in notebook(path)['cells'] if c['cell_type'] == 'code')
                else:
                    code = text
                self.assertIsNone(bare_except.search(code), f'{path.name}: bare except')
                self.assertIsNone(silent_except.search(code), f'{path.name}: silent except/pass')


if __name__ == '__main__':
    unittest.main()
