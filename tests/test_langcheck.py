"""Offline tests: a tiny fixture dictionary stands in for the LibreOffice ones and the
network layers (LanguageTool, Korektor) are mocked. Run: python -m unittest -v

If the real dictionaries are in ./dicts (python fetch_dicts.py), ExamplesTest also
checks the Hunspell findings on the bundled examples.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DICTS = os.path.join(ROOT, 'tests', 'fixtures', 'dicts')
REAL_DICTS = os.path.join(ROOT, 'dicts')
sys.path.insert(0, ROOT)

import langcheck  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(lt_matches=None, korektor=None, fail=None):
    """Build a urlopen stand-in. lt_matches: text -> LanguageTool matches; korektor: text -> corrected."""
    calls = []

    def _open(url, data=None, timeout=None):
        form = urllib.parse.parse_qs(data.decode()) if data else {}
        calls.append((url, form))
        if fail:
            raise fail
        if url == langcheck.KOREKTOR_URL:
            text = form['data'][0]
            return FakeResponse(json.dumps({'result': (korektor or {}).get(text, text)}).encode())
        text = form['text'][0]
        return FakeResponse(json.dumps({'matches': (lt_matches or {}).get(text, [])}).encode())
    _open.calls = calls
    return _open


def run(argv, dicts=FIXTURE_DICTS, opener=None, stdin=None):
    """Run main() and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = {'LANGTOOLS_DICTS': dicts, 'LT_URL': 'http://lt.test/v2/check'}
    opener = opener or fake_urlopen(fail=urllib.error.URLError('connection refused'))
    with mock.patch.dict(os.environ, env), mock.patch('urllib.request.urlopen', opener), \
            mock.patch('sys.stdin', io.StringIO(stdin or '')), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = langcheck.main(argv)
        except SystemExit as e:  # argparse
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        langcheck._DICT_CACHE.clear()

    def write(self, name, content):
        p = os.path.join(self.tmp.name, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)
        return p


class ParsingTest(TempFiles):
    def test_norm_lang(self):
        self.assertEqual(langcheck.norm_lang('cs-CZ'), 'cs')
        self.assertEqual(langcheck.norm_lang('CZ'), 'cs')
        self.assertEqual(langcheck.norm_lang('de_AT'), 'de')
        self.assertEqual(langcheck.norm_lang('en-US'), 'en')
        self.assertEqual(langcheck.norm_lang(None), '')

    def test_markdown_strips_markup(self):
        md = '# Title\n\nSee [the app](https://x.y) and `code`.\n\n```\nskip me\n```\n\n- **Bold** item'
        texts = langcheck.parse_text(md)
        self.assertEqual(texts, ['Title', 'See the app and .', 'Bold item'])

    def test_html_visible_text_attributes_and_lang(self):
        p = self.write('a.html', '<html lang="sk-SK"><head><title>x</title></head><body><h1>Ahoj</h1>'
                                 '<script>var nope;</script><img alt="Obrázok"><p>Text <b>tučne</b></p></body></html>')
        lang, texts = langcheck.collect(p)
        self.assertEqual(lang, 'sk')
        self.assertEqual(texts, ['Ahoj', 'Obrázok', 'Text tučne'])

    def test_json_known_keys_and_lang(self):
        p = self.write('a.json', json.dumps({'lang': 'pl', 'headline': 'H', 'copy': ['A', 'B'], 'url': 'skip',
                                             'nested': {'cta': 'C'}}))
        self.assertEqual(langcheck.collect(p), ('pl', ['H', 'A', 'B', 'C']))

    def test_json_all_strings_skips_lang_value(self):
        p = self.write('a.json', json.dumps({'lang': 'pl', 'k': 'one', 'list': ['two']}))
        self.assertEqual(langcheck.collect(p, all_strings=True), ('pl', ['one', 'two']))

    def test_json_with_bom(self):
        p = os.path.join(self.tmp.name, 'bom.json')
        with open(p, 'w', encoding='utf-8-sig') as f:
            f.write('{"lang": "en", "text": "hi"}')
        self.assertEqual(langcheck.collect(p), ('en', ['hi']))


class CliTest(TempFiles):
    def test_help_exits_zero(self):
        code, out, _ = run(['--help'])
        self.assertEqual(code, 0)
        self.assertIn('Exit codes', out)

    def test_clean_text(self):
        code, out, _ = run(['--lang', 'en', '--no-languagetool', 'Your money in one place'])
        self.assertEqual(code, 0, out)
        self.assertTrue(out.rstrip().endswith('clean'))

    def test_typo_is_a_finding(self):
        code, out, _ = run(['--lang', 'en', '--no-languagetool', 'Your mony in one place'])
        self.assertEqual(code, 1)
        self.assertIn('"mony" -> money', out)

    def test_ignore_file_and_builtin_words(self):
        words = self.write('words.txt', '# brand\nZorbly\n')
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '--ignore', words, 'Get zorbly on iOS'])
        self.assertEqual(code, 0, out)

    def test_missing_lang_is_usage_error(self):
        code, _, err = run(['Some text'])
        self.assertEqual(code, 2)
        self.assertIn('--lang', err)

    def test_lang_without_value_is_usage_error(self):
        code, _, _ = run(['--lang'])
        self.assertEqual(code, 2)

    def test_unsupported_language_is_usage_error(self):
        code, _, err = run(['--lang', 'xx', 'foo'])
        self.assertEqual(code, 2)
        self.assertIn('not supported', err)

    def test_missing_file_is_usage_error_not_text(self):
        code, _, err = run(['--lang', 'en', 'copy/typo-in-path.json'])
        self.assertEqual(code, 2)
        self.assertIn('file not found', err)

    def test_invalid_json_is_usage_error(self):
        code, _, err = run([self.write('bad.json', '{"lang": "en",')])
        self.assertEqual(code, 2)
        self.assertIn('not valid JSON', err)

    def test_non_utf8_file_is_usage_error(self):
        p = os.path.join(self.tmp.name, 'latin2.txt')
        with open(p, 'wb') as f:
            f.write('Získej odměnu'.encode('cp1250'))
        code, _, err = run(['--lang', 'cs', p])
        self.assertEqual(code, 2)
        self.assertIn('codec', err)

    def test_json_without_copy_keys_is_usage_error(self):
        """A JSON file with no known copy keys must not pass as clean."""
        code, _, err = run([self.write('strings.json', json.dumps({'lang': 'en', 'welcome_msg': 'Hi'}))])
        self.assertEqual(code, 2)
        self.assertIn('--all-strings', err)

    def test_several_files_with_their_own_languages(self):
        a = self.write('a.json', json.dumps({'lang': 'en', 'headline': 'Invite a frend'}))
        b = self.write('b.html', '<html lang="cs"><body><p>Pozvi kamaráda</p></body></html>')
        code, out, _ = run(['--json', '--no-languagetool', a, b])
        self.assertEqual(code, 1)
        rep = json.loads(out)
        self.assertEqual(set(rep['checked']), {'en', 'cs'})
        self.assertEqual([(f['found'], f['source']) for f in rep['findings']], [('frend', a)])

    def test_stdin(self):
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '-'], stdin='Get a frend\n\nYour money')
        self.assertEqual(code, 1)
        self.assertIn('frend', out)

    def test_no_dictionary_and_no_server_fails_closed(self):
        """Nothing could check the text: that must not look like clean copy."""
        code, out, _ = run(['--lang', 'de', 'Alles gut'], dicts=self.tmp.name)
        self.assertEqual(code, 3)
        self.assertIn('NOT CHECKED', out)

    def test_missing_languagetool_is_a_note_unless_strict(self):
        code, out, _ = run(['--lang', 'en', 'Your money'])
        self.assertEqual(code, 0)
        self.assertIn('[languagetool]', out)
        self.assertIn('grammar NOT checked', out)
        code, _, _ = run(['--lang', 'en', '--strict', 'Your money'])
        self.assertEqual(code, 3)

    def test_findings_win_over_unchecked(self):
        code, _, _ = run(['--lang', 'en', '--strict', 'Your mony'])
        self.assertEqual(code, 1)


class LanguageToolTest(TempFiles):
    MATCH = {'offset': 0, 'length': 4, 'message': 'Possible agreement error',
             'rule': {'id': 'AGREEMENT'}, 'replacements': [{'value': 'Your'}, {'value': 'You'}]}

    def test_match_becomes_finding_with_rule(self):
        opener = fake_urlopen(lt_matches={'Youre money': [self.MATCH]})
        code, out, _ = run(['--lang', 'en', '--json', 'Youre money'], opener=opener)
        rep = json.loads(out)
        self.assertEqual(code, 1)
        lt = [f for f in rep['findings'] if f['tool'] == 'languagetool']
        self.assertEqual(lt[0]['rule'], 'AGREEMENT')
        self.assertEqual(lt[0]['suggest'], ['Your', 'You'])
        self.assertEqual(rep['checked']['en']['ran'], ['hunspell', 'languagetool'])
        url, form = opener.calls[0]
        self.assertEqual(url, 'http://lt.test/v2/check')
        self.assertEqual(form['language'], ['en-GB'])
        self.assertIn('WHITESPACE_RULE', form['disabledRules'][0])

    def test_http_error_is_reported(self):
        err = urllib.error.HTTPError('http://lt.test', 429, 'Too Many Requests', {}, None)
        code, out, _ = run(['--lang', 'en', 'Your money'], opener=fake_urlopen(fail=err))
        self.assertEqual(code, 0)
        self.assertIn('HTTP 429', out)

    def test_czech_never_goes_to_languagetool(self):
        opener = fake_urlopen()
        run(['--lang', 'cs', 'Pozvi kamaráda'], opener=opener)
        self.assertEqual(opener.calls, [])


class KorektorTest(TempFiles):
    def test_off_by_default(self):
        opener = fake_urlopen()
        code, _, _ = run(['--lang', 'cs', 'Pozvi kamaráda'], opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(opener.calls, [])

    def test_online_word_by_word_and_post(self):
        opener = fake_urlopen(korektor={'Pozvi kamarada': 'Pozvi kamaráda'})
        code, out, _ = run(['--lang', 'cs', '--online', '--json', 'Pozvi kamarada'], opener=opener)
        rep = json.loads(out)
        self.assertEqual(code, 1)
        k = [f for f in rep['findings'] if f['tool'] == 'korektor']
        self.assertEqual([(f['found'], f['suggest']) for f in k], [('kamarada', ['kamaráda'])])
        url, form = opener.calls[0]
        self.assertEqual(url, langcheck.KOREKTOR_URL)
        self.assertEqual(form['data'], ['Pozvi kamarada'])  # sent as POST body, not in the URL

    def test_no_online_overrides_online(self):
        opener = fake_urlopen()
        run(['--lang', 'cs', '--online', '--no-online', 'Pozvi kamaráda'], opener=opener)
        self.assertEqual(opener.calls, [])

    def test_unreachable_falls_back_to_dictionary(self):
        code, out, _ = run(['--lang', 'cs', '--online', 'Pozvi kamaráda'])
        self.assertEqual(code, 0)
        self.assertIn('Czech checked by dictionary only', out)


EXAMPLE_DICTS = ('cs_CZ', 'sk_SK', 'pl_PL', 'de_DE_frami')


@unittest.skipUnless(all(os.path.exists(os.path.join(REAL_DICTS, n + '.dic')) for n in EXAMPLE_DICTS),
                     'run python fetch_dicts.py first')
class ExamplesTest(TempFiles):
    """The bundled examples with the real LibreOffice dictionaries, network layers off."""

    def hunspell_words(self, *argv):
        code, out, _ = run(['--json', '--no-languagetool', *argv], dicts=REAL_DICTS)
        return code, [f['found'] for f in json.loads(out)['findings']]

    def test_planted_mistakes_are_caught(self):
        ex = os.path.join(ROOT, 'examples')
        self.assertEqual(self.hunspell_words(os.path.join(ex, 'ad-copy.json')), (1, ['kamarada', 'ziskej', 'odmenu']))
        self.assertEqual(self.hunspell_words(os.path.join(ex, 'landing-pl.html')), (1, ['nagrode', 'Sprawdz']))
        self.assertEqual(self.hunspell_words('--lang', 'de', os.path.join(ex, 'newsletter-de.md')), (1, ['Weihnachen']))

    def test_clean_example_passes(self):
        self.assertEqual(self.hunspell_words('--lang', 'sk', os.path.join(ROOT, 'examples', 'clean-sk.txt')), (0, []))


if __name__ == '__main__':
    unittest.main()
