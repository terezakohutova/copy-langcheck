"""Tests for the 1.3.0 fixes from the second outside review: exact positions through the
extraction, placeholders for skipped markup, front matter, translate="no", path handling,
retries, LanguageTool concurrency, suggestion limits, the exit code when findings and a
missing layer meet, and the LanguageTool container script used by the action."""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from tests.test_langcheck import (
    REFUSED,
    ROOT,
    TempFiles,
    _Return,
    _returning,
    fake_urlopen,
    langcheck,
    lt_match,
    run,
    run_json,
    texts_of,
)

P = langcheck.PLACEHOLDER
FIXTURES = os.path.join(ROOT, 'tests', 'fixtures', 'dicts')


def found_at(rep):
    return [(f['found'], f['line'], f['col']) for f in rep['findings']]


# ------------------------------------------------------------ 3: findings and a missing layer

class IncompleteWithFindingsTest(TempFiles):
    def test_strict_findings_with_a_missing_layer_is_exit_3_and_lists_both(self):
        code, out, _ = run(['--lang', 'en', 'Your mony'])          # LanguageTool refused
        self.assertEqual(code, 3)
        self.assertIn('"mony"', out)
        self.assertIn('1 findings', out)
        self.assertIn('NOT CHECKED', out)

    def test_json_report_keeps_the_findings(self):
        code, rep, _ = run_json(['--lang', 'en', 'Your mony'])
        self.assertEqual((code, rep['exit_code']), (3, 3))
        self.assertEqual([f['found'] for f in rep['findings']], ['mony'])

    def test_lenient_findings_with_a_missing_layer_is_exit_1(self):
        code, out, _ = run(['--lang', 'en', '--lenient', 'Your mony'])
        self.assertEqual(code, 1)
        self.assertIn('[languagetool]', out)

    def test_findings_with_every_layer_run_is_exit_1(self):
        code, out, _ = run(['--lang', 'en', 'Your mony'], opener=fake_urlopen())
        self.assertEqual(code, 1)
        self.assertFalse(out.rstrip().endswith('clean'), out)
        self.assertTrue(out.rstrip().endswith('1 findings'), out)


# ------------------------------------------------------------ 4: exact positions

class ExactPositionTest(TempFiles):
    """Each source has the planted word twice, the first time inside markup that is not checked."""

    def last(self, raw, word='frend'):
        i = raw.rindex(word)
        return [(word, raw[:i].count('\n') + 1, i - raw.rfind('\n', 0, i))]

    def check(self, name, raw, *flags):
        p = self.write(name, raw)
        code, rep, _ = run_json(['--no-languagetool', *flags, p])
        self.assertEqual(found_at(rep), self.last(raw))

    def test_html_word_also_in_an_href(self):
        self.check('a.html', '<html lang="en"><body>\n<p>Get <a href="frend.html">the money</a> and a frend</p>\n'
                             '</body></html>')

    def test_html_word_also_in_a_skipped_element(self):
        self.check('b.html', '<html lang="en"><body><p><code>frend</code> and a frend</p></body></html>')

    def test_html_entity_before_the_word(self):
        self.check('c.html', '<html lang="en"><body><p>Your money &amp; a friend &mdash; frend</p></body></html>')

    def test_html_attribute_value(self):
        self.check('d.html', '<html lang="en"><body>\n  <img src="frend.png" alt="A frend">\n</body></html>')

    def test_markdown_inline_code_before_the_word(self):
        self.check('e.md', 'Get `frend` and a frend\n', '--lang', 'en')

    def test_markdown_link_url_before_the_word(self):
        self.check('f.md', 'Get [the money](https://x.y/frend) and a frend\n', '--lang', 'en')

    def test_every_character_maps_into_the_source(self):
        raw = '<p>Get\n   <b>a</b>   frend &amp; mony</p>'
        s = langcheck.parse_html(raw, 'x.html')[0]
        self.assertEqual(s.text, 'Get a frend & mony')
        for word in ('Get', 'a', 'frend', 'mony'):
            i = s.text.index(word)
            line, col = s.locate(i, word)
            src = raw.split('\n')[line - 1]
            self.assertEqual(src[col - 1:col - 1 + len(word)], word, word)




# ------------------------------------------------------------ 5: placeholders for skipped markup

class PlaceholderTest(TempFiles):
    def test_markdown_inline_code_becomes_a_placeholder(self):
        self.assertEqual(texts_of(langcheck.parse_text('Run `npm ci` before you push.')), [f'Run {P} before you push.'])

    def test_html_inline_code_becomes_a_placeholder(self):
        s = langcheck.parse_html('<p>Press <kbd>Enter</kbd> to send <code>x</code>.</p>')
        self.assertEqual(texts_of(s), [f'Press {P} to send {P}.'])

    def test_text_that_is_only_code_is_dropped(self):
        self.assertEqual(texts_of(langcheck.parse_html('<li><code>npm ci</code></li><li>Done</li>')), ['Done'])
        self.assertEqual(texts_of(langcheck.parse_text('`npm ci`\n\nDone')), ['Done'])

    def test_scripts_and_blocks_leave_no_placeholder(self):
        s = langcheck.parse_html('<p>Hello<script>x()</script> world<svg><text>a</text></svg></p><pre>code</pre>')
        self.assertEqual(texts_of(s), ['Hello world'])

    def test_placeholder_is_not_a_finding(self):
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', '-'], stdin='Get `frendly` money')
        self.assertEqual((code, rep['findings']), (0, []))


# ------------------------------------------------------------ 6: front matter, comments

class MarkdownTest(TempFiles):
    def test_front_matter_is_skipped_except_title_and_description(self):
        md = ('---\nlayout: post\ntitle: "Your money"\nslug: frend-mony\ndescription: Get a frend\n'
              'tags: [a, b]\n---\n\nBody text\n')
        s = langcheck.parse_text(md, 'p.md')
        self.assertEqual(texts_of(s), ['Your money', 'Get a frend', 'Body text'])
        self.assertEqual((s[1].locate(6, 'frend')), (5, 20))

    def test_front_matter_only_at_the_very_start(self):
        md = 'Intro\n\n---\ntitle: kept as text\n---\n'
        self.assertEqual(texts_of(langcheck.parse_text(md)), ['Intro', '--- title: kept as text ---'])

    def test_multi_line_html_comment_is_skipped(self):
        md = 'Before\n\n<!--\nfrend\nmony\n-->\n\nAfter <!-- inline --> end\n'
        self.assertEqual(texts_of(langcheck.parse_text(md)), ['Before', 'After end'])

    def test_front_matter_findings_are_reported(self):
        p = self.write('fm.md', '---\ntitle: Get a frend\nslug: zzqx\n---\nYour money\n')
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', p])
        self.assertEqual(found_at(rep), [('frend', 2, 14)])


# ------------------------------------------------------------ 7: translate="no"

class NoTranslateTest(TempFiles):
    def test_translate_no_and_notranslate_are_skipped(self):
        s = langcheck.parse_html('<p>Get <span translate="no">Zorbly</span> and '
                                 '<b class="x notranslate">Qwix</b> now</p>'
                                 '<div translate="no"><p>Zorbly Ltd</p></div><p>End</p>')
        self.assertEqual(texts_of(s), [f'Get {P} and {P} now', 'End'])

    def test_translate_yes_inside_no_is_checked(self):
        s = langcheck.parse_html('<div translate="no">Brand <span translate="yes">and copy</span></div>')
        self.assertEqual(texts_of(s), ['and copy'])

    def test_attributes_of_a_no_translate_element_are_skipped(self):
        s = langcheck.parse_html('<div class="notranslate"><img alt="Zorbly logo"></div><img alt="A logo">')
        self.assertEqual(texts_of(s), ['A logo'])

    def test_end_to_end(self):
        p = self.write('n.html', '<html lang="en"><body><p>Get <span translate="no">Zorbly</span> money</p>'
                                 '</body></html>')
        self.assertEqual(run(['--no-languagetool', p])[0], 0)


# ------------------------------------------------------------ 8: paths and --text

class PathTest(TempFiles):
    def test_anything_path_like_must_exist(self):
        for arg in ('copy/ads', 'copy\\ads', 'ads.yaml', 'ad-copy.JSONL', 'notes.txt', 'vis.zkus'):
            code, _, err = run(['--lang', 'en', arg])
            self.assertEqual(code, 2, arg)
            self.assertIn('file not found', err)

    def test_text_flag_is_copy(self):
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '--text', 'see notes.txt or a/b',
                            '--text', 'Get a frend'])
        self.assertEqual(code, 1)
        self.assertIn('<text2>:1:7: [hunspell] "frend"', out)

    def test_text_that_looks_like_a_path_via_text(self):
        self.assertEqual(run(['--lang', 'en', '--no-languagetool', '--text', 'example.com'])[0], 0)

    def test_positional_copy_still_works_with_a_notice(self):
        code, out, err = run(['--lang', 'en', '--no-languagetool', 'Your money'])
        self.assertEqual(code, 0)
        self.assertIn('--text', err)
        self.assertIn('deprecated', err)

    def test_no_input_at_all_is_usage_error(self):
        self.assertEqual(run(['--lang', 'en'])[0], 2)

    def test_files_and_text_together(self):
        p = self.write('a.txt', 'Your money')
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', p, '--text', 'Get a frend'])
        self.assertEqual([f['source'] for f in rep['findings']], ['<text1>'])


# ------------------------------------------------------------ 9: retries

class RetryTest(TempFiles):
    def calls_for(self, error):
        def script(n, url, form):
            if n == 1:
                raise error
        opener = fake_urlopen(script=script)
        run(['--lang', 'en', 'Your money'], opener=opener)
        return len(opener.calls)

    def test_retried_on_network_errors_5xx_and_timeouts(self):
        self.assertEqual(self.calls_for(urllib.error.HTTPError('u', 503, 'x', {}, None)), 2)
        self.assertEqual(self.calls_for(urllib.error.URLError('reset')), 2)
        self.assertEqual(self.calls_for(TimeoutError('timed out')), 2)
        self.assertEqual(self.calls_for(ConnectionResetError()), 2)

    def test_not_retried_on_4xx_or_a_bad_answer(self):
        self.assertEqual(self.calls_for(urllib.error.HTTPError('u', 400, 'x', {}, None)), 1)
        self.assertEqual(self.calls_for(urllib.error.HTTPError('u', 429, 'x', {}, None)), 1)
        self.assertEqual(self.calls_for(ValueError('not JSON')), 1)

    def test_korektor_per_text_fallback_is_capped(self):
        def merge_lines(n, url, form):
            if '\n' in form['data']:
                raise _Return(json.dumps({'result': 'one line only'}))
        opener = _returning(fake_urlopen(script=merge_lines))
        n = langcheck.KOREKTOR_FALLBACK_MAX + 5
        p = self.write('cs.txt', '\n\n'.join(f'Text číslo {i}' for i in range(n)))
        with mock.patch.object(langcheck, 'hunspell', lambda t, *a, **k: langcheck.LayerResult()):
            code, rep, _ = run_json(['--lang', 'cs', '--online', p], opener=opener)
        self.assertEqual(len(opener.calls), 1 + langcheck.KOREKTOR_FALLBACK_MAX)
        self.assertEqual(code, 3)
        self.assertIn(f'5 of {n} texts not checked', rep['checked']['cs-CZ']['skipped']['korektor'])


# ------------------------------------------------------------ 10: LanguageTool concurrency

class _LTHandler(http.server.BaseHTTPRequestHandler):
    active = 0
    peak = 0
    seen: list[str] = []
    lock = threading.Lock()

    def do_POST(self):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        body = self.rfile.read(int(self.headers['Content-Length'])).decode()
        text = urllib.parse.parse_qs(body)['text'][0]
        time.sleep(0.05)
        with cls.lock:
            cls.seen.append(text)
            cls.active -= 1
        if text.startswith('fail'):
            self.send_response(500)
            self.end_headers()
            return
        matches = [lt_match(0, 3, rule='R_' + text.split()[-1])] if text.startswith('bad') else []
        out = json.dumps({'matches': matches}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class ConcurrencyTest(TempFiles):
    def setUp(self):
        super().setUp()
        _LTHandler.active = _LTHandler.peak = 0
        _LTHandler.seen = []
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _LTHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/v2/check'

    def check(self, texts):
        with mock.patch.dict(os.environ, {'LT_URL': self.url}), mock.patch.object(langcheck, 'RETRY_DELAY', 0):
            return langcheck.languagetool(texts, 'en-GB', lambda w: False)

    def test_bounded_concurrency_and_every_finding_kept(self):
        texts = [f'bad text {i}' for i in range(10)] + [f'ok text {i}' for i in range(6)]
        res = self.check(texts)
        self.assertEqual(sorted(_LTHandler.seen), sorted(texts))
        self.assertGreater(_LTHandler.peak, 1)
        self.assertLessEqual(_LTHandler.peak, langcheck.LT_WORKERS)
        self.assertEqual(sorted(f.rule for f in res.findings), sorted(f'R_{i}' for i in range(10)))
        self.assertEqual([f.text for f in res.findings], [t for t in texts if t.startswith('bad')])   # input order

    def test_all_errors_are_aggregated(self):
        res = self.check(['fail one', 'ok two', 'fail three'])
        self.assertEqual(sorted(res.unchecked), ['fail one', 'fail three'])
        self.assertIn('HTTP 500', res.error)
        self.assertIn('2 texts', res.error)

    def test_server_gone_stops_new_requests(self):
        texts = ['Your money'] + [f'Text {i}' for i in range(20)]

        def only_first(n, url, form):
            if form['text'] != texts[0]:
                raise REFUSED
        opener = fake_urlopen(script=only_first)
        p = self.write('t.txt', '\n\n'.join(texts))
        code, rep, _ = run_json(['--lang', 'en', p], opener=opener)
        self.assertEqual(code, 3)
        self.assertLessEqual(len(opener.calls), 1 + 2 * langcheck.LT_WORKERS)
        self.assertIn('not reachable', rep['checked']['en-GB']['skipped']['languagetool'])


# ------------------------------------------------------------ 11: JSON positions

class JsonScannerTest(TempFiles):
    def test_escapes_and_duplicates(self):
        raw = ('{"lang": "en",\n "headline": "Caf\\u00e9 \\"frend\\"",\n "x": {"headline": "frend"},\n'
               ' "cta": ["ok", "a \\n mony"]}')
        s = langcheck.parse_json(raw, 'a.json')
        self.assertEqual(texts_of(s), ['Café "frend"', 'frend', 'ok', 'a mony'])
        self.assertEqual(s[0].locate(6, 'frend'), (2, 27))
        self.assertEqual(s[1].locate(0, 'frend'), (3, 21))
        self.assertEqual(s[3].locate(2, 'mony'), (4, 22))

    def test_same_value_under_a_non_copy_key_first(self):
        raw = '{"lang": "en", "id": "Get a frend", "headline": "Get a frend"}'
        p = self.write('b.json', raw)
        code, rep, _ = run_json(['--no-languagetool', p])
        self.assertEqual(found_at(rep), [('frend', 1, raw.rindex('frend') + 1)])

    def test_value_used_as_a_key_elsewhere(self):
        raw = '{"Get a frend": 1, "lang": "en", "text": "Get a frend"}'
        s = langcheck.parse_json(raw)
        self.assertEqual(s[0].locate(6, 'frend'), (1, raw.rindex('frend') + 1))

    def test_scanner_paths(self):
        found = langcheck.json_string_offsets('{"a": ["x", {"b": "y"}], "c": "z\\u0041"}')
        self.assertEqual({k: v[0] for k, v in found.items()}, {('a', 0): 'x', ('a', 1, 'b'): 'y', ('c',): 'zA'})


# ------------------------------------------------------------ 2: suggestions

class SuggestTest(TempFiles):
    def setUp(self):
        super().setUp()
        langcheck._SUGGEST_CACHE.clear()

    def test_no_suggest(self):
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', '--no-suggest', '--text', 'Get a frend'])
        self.assertEqual((code, rep['findings'][0]['suggest']), (1, []))

    def test_only_the_first_n_unknown_words_get_suggestions(self):
        words = ' '.join(f'zq{chr(97 + i)}{chr(97 + j)}' for i in range(5) for j in range(5))   # 25 unknown
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', '--suggest-limit', '3', '--text', words])
        self.assertEqual(len(rep['findings']), 25)
        self.assertTrue(any('suggestions for the first 3' in n and '22 more' in n for n in rep['notes']), rep['notes'])
        self.assertEqual(code, 1)                    # a skipped suggestion is not a skipped check

    def test_suggest_called_once_per_word_and_limit_respected(self):
        calls = []
        with mock.patch.dict(os.environ, {'LANGTOOLS_DICTS': FIXTURES}):
            d = langcheck.load_dictionary('en-GB')[0]
            real = d.suggest

            def counting(w):
                calls.append(w)
                return real(w)
            with mock.patch.object(d, 'suggest', counting, create=True):
                langcheck.hunspell(['frend frend mony zzqa zzqb'], 'en-GB', lambda w: False, suggest_limit=2)
                res = langcheck.hunspell(['frend'], 'en-GB', lambda w: False, suggest_limit=2)
        self.assertEqual(calls, ['frend', 'mony'])          # cached across calls, nothing past the limit
        self.assertIn('friend', res.findings[0].suggest)


# ------------------------------------------------------------ 1, 14: action and CI

def _git_bash():
    if os.name != 'nt':
        return shutil.which('bash')
    for p in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe'):
        if os.path.exists(p):
            return p
    return None


BASH = _git_bash()
STUB_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  port) echo "127.0.0.1:49170" ;;
  run) [ -n "$DOCKER_RUN_FAILS" ] && exit 125; echo cid ;;
esac
exit 0
'''
STUB_CURL = r'''#!/usr/bin/env bash
for a in "$@"; do url="$a"; done
echo "curl $url" >> "$STUB_LOG"
for p in $HEALTHY_PORTS; do case "$url" in *":$p/"*) exit 0 ;; esac; done
exit 7
'''


@unittest.skipUnless(BASH, 'bash not found')
class ContainerScriptTest(unittest.TestCase):
    """scripts/languagetool.sh with docker and curl replaced by stubs that log their calls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, body in (('docker', STUB_DOCKER), ('curl', STUB_CURL)):
            path = os.path.join(self.tmp, name)
            with open(path, 'w', newline='\n') as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp, 'log')

    def sh(self, body, **env):
        script = os.path.join(ROOT, 'scripts', 'languagetool.sh').replace('\\', '/')
        tmp = self.tmp.replace('\\', '/')
        if os.name == 'nt' and re.match(r'^[A-Za-z]:', tmp):
            tmp = '/' + tmp[0].lower() + tmp[2:]
        full = f'export PATH="{tmp}:$PATH"; LT_POLL=0; source "{script}"\n{body}'
        e = dict(os.environ, STUB_LOG=self.log, GITHUB_RUN_ID='777', GITHUB_RUN_ATTEMPT='2',
                 GITHUB_ACTION='__self', INPUT_FILES='a.json', HEALTHY_PORTS='')
        e.update(env)
        r = subprocess.run([BASH, '-c', full], env=e, capture_output=True, text=True, timeout=60)
        log = ''
        if os.path.exists(self.log):
            with open(self.log, encoding='utf-8') as f:
                log = f.read()
        return r, log

    def test_reuses_a_healthy_server_on_8081(self):
        r, log = self.sh('lt_start && echo "URL=$LT_URL C=$LT_CONTAINER"; lt_stop', HEALTHY_PORTS='8081')
        self.assertIn('URL=http://localhost:8081/v2/check C=', r.stdout)
        self.assertNotIn('docker', log)

    def test_starts_a_uniquely_named_container_on_a_free_port_and_removes_it(self):
        r, log = self.sh('lt_start && echo "URL=$LT_URL C=$LT_CONTAINER"; lt_stop', HEALTHY_PORTS='49170')
        name = re.search(r'C=(\S+)', r.stdout).group(1)
        self.assertRegex(name, r'^copy-langcheck-lt-777-2-[0-9a-f]{12}$')
        self.assertIn('URL=http://127.0.0.1:49170/v2/check', r.stdout)
        self.assertIn(f'docker run -d --rm --name {name} -p 127.0.0.1::8010 erikvl87/languagetool:6.8@sha256:', log)
        self.assertIn(f'docker rm -f {name}', log.splitlines()[-1])
        self.assertNotIn('8081', log.split('curl', 2)[-1])   # the started one is polled on its own port

    def test_different_inputs_give_different_names(self):
        a, _ = self.sh('lt_name', INPUT_FILES='a.json')
        b, _ = self.sh('lt_name', INPUT_FILES='b.json')
        self.assertNotEqual(a.stdout.strip(), b.stdout.strip())

    def test_trap_removes_the_container_when_the_step_fails(self):
        r, log = self.sh('trap lt_stop EXIT; lt_start; exit 1', HEALTHY_PORTS='49170')
        self.assertEqual(r.returncode, 1)
        self.assertIn('docker rm -f copy-langcheck-lt-777-2-', log)

    def test_server_that_never_comes_up_still_gets_removed(self):
        r, log = self.sh('trap lt_stop EXIT; lt_start || echo NOT-UP')
        self.assertIn('NOT-UP', r.stdout)
        self.assertIn('docker rm -f copy-langcheck-lt-', log)


class ActionLayoutTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'action.yml'), encoding='utf-8') as f:
            self.action = f.read()

    def test_no_fixed_container_name_and_cleanup_in_the_same_step(self):
        self.assertNotIn('--name languagetool', self.action)
        check = self.action.split('id: check', 1)[1]
        self.assertIn('trap lt_stop EXIT', check)
        self.assertIn('lt_start', check)
        self.assertLess(check.index('trap lt_stop EXIT'), check.index('lt_start'))

    def test_dictionary_cache_key_follows_fetch_dicts_not_langcheck(self):
        key_step = self.action.split('Dictionary cache key', 1)[1].split('- uses', 1)[0]
        self.assertIn('fetch_dicts.py', key_step)
        self.assertNotIn('langcheck.py', key_step)
        self.assertIn('REF', key_step)
        with open(os.path.join(ROOT, '.github', 'workflows', 'tests.yml'), encoding='utf-8') as f:
            wf = f.read()
        self.assertNotIn("'langcheck.py'", wf)
        self.assertIn("hashFiles('fetch_dicts.py')", wf)

    def test_korektor_smoke_is_manual_only(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'korektor-smoke.yml'), encoding='utf-8') as f:
            wf = f.read()
        on = wf.split('on:', 1)[1].split('jobs:', 1)[0]
        self.assertIn('workflow_dispatch', on)
        self.assertNotIn('push', on)
        self.assertNotIn('pull_request', on)
        self.assertIn('--online', wf)


class PackagingTest(unittest.TestCase):
    def test_spylls_is_pinned_with_hashes(self):
        with open(os.path.join(ROOT, 'requirements.txt'), encoding='utf-8') as f:
            req = f.read()
        self.assertIn('spylls==0.1.7', req)
        self.assertEqual(len(re.findall(r'--hash=sha256:[0-9a-f]{64}', req)), 2)

    def test_version(self):
        self.assertEqual(langcheck.__version__, '1.3.0')
        with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as f:
            self.assertIn('## 1.3.0', f.read())

    def test_readme_headline(self):
        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            head = f.read(1500)
        self.assertIn('grammar for SK/PL/DE/EN via LanguageTool', head)
        self.assertIn('Czech spelling, grammar via optional Korektor', head)


if __name__ == '__main__':
    unittest.main()
