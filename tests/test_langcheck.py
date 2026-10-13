"""Offline tests: tiny fixture dictionaries stand in for the LibreOffice ones and the
network layers (LanguageTool, Korektor) are mocked. Run: python -m unittest -v

If the real dictionaries are in ./dicts (python fetch_dicts.py), ExamplesTest also
checks the Hunspell findings on the bundled examples.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
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

import fetch_dicts  # noqa: E402
import langcheck  # noqa: E402

REFUSED = urllib.error.URLError('connection refused')


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(lt_matches=None, korektor=None, fail=None, script=None):
    """urlopen stand-in. lt_matches: text -> LanguageTool matches; korektor: text -> corrected
    (applied line by line, as the real service does); fail: raise this on every call;
    script: function(n, url, form) called first, may raise to simulate a failure on call n."""
    calls = []

    def _open(url, data=None, timeout=None):
        form = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode(), keep_blank_values=True).items()}
        calls.append((url, form))
        if fail:
            raise fail
        if script:
            script(len(calls), url, form)
        if url == langcheck.KOREKTOR_URL:
            fixed = '\n'.join((korektor or {}).get(line, line) for line in form['data'].split('\n'))
            return FakeResponse(json.dumps({'result': fixed + '\n'}).encode())
        return FakeResponse(json.dumps({'matches': (lt_matches or {}).get(form['text'], [])}).encode())
    _open.calls = calls
    return _open


def run(argv, dicts=FIXTURE_DICTS, opener=None, stdin=None):
    """Run main() and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = {'LANGTOOLS_DICTS': dicts, 'LT_URL': 'http://lt.test/v2/check'}
    opener = opener or fake_urlopen(fail=REFUSED)
    with mock.patch.dict(os.environ, env), mock.patch('urllib.request.urlopen', opener), \
            mock.patch.object(langcheck, 'RETRY_DELAY', 0), \
            mock.patch('sys.stdin', io.StringIO(stdin or '')), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = langcheck.main(argv)
        except SystemExit as e:  # argparse
            code = e.code
    return code, out.getvalue(), err.getvalue()


def run_json(argv, **kw):
    code, out, err = run(['--json', *argv], **kw)
    return code, json.loads(out) if out.strip() else None, err


def lt_match(offset, length, rule='AGREEMENT', message='Possible agreement error', repl=()):
    return {'offset': offset, 'length': length, 'message': message, 'rule': {'id': rule},
            'replacements': [{'value': r} for r in repl]}


def texts_of(snippets):
    return [s.text for s in snippets]


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        langcheck._DICT_CACHE.clear()

    def write(self, name, content):
        p = os.path.join(self.tmp.name, name)
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        return p


# ------------------------------------------------------------ 1, 2: language tags

class LanguageTagTest(TempFiles):
    def test_bcp47_tags(self):
        n = langcheck.norm_lang
        self.assertEqual(n('cs'), 'cs-CZ')
        self.assertEqual(n('cs-CZ'), 'cs-CZ')
        self.assertEqual(n('de'), 'de-DE')
        self.assertEqual(n('de_AT'), 'de-AT')
        self.assertEqual(n('de-ch'), 'de-CH')
        self.assertEqual(n('en'), 'en-GB')
        self.assertEqual(n('en-us'), 'en-US')
        self.assertEqual(n('EN_GB'), 'en-GB')

    def test_country_codes_are_not_languages(self):
        for code, hint in (('uk', 'Ukrainian'), ('cz', 'language tag for Czech is cs'), ('at', 'de-AT'),
                           ('ch', 'de-CH'), ('gb', 'en-GB')):
            with self.assertRaises(langcheck.UnsupportedLanguage) as e:
                langcheck.norm_lang(code)
            self.assertIn(hint, str(e.exception))

    def test_unsupported_variants_and_garbage(self):
        for tag in ('de-LU', 'en-AU', 'fr', 'english', '', 'cs-SK'):
            with self.assertRaises(langcheck.UnsupportedLanguage):
                langcheck.norm_lang(tag)

    def test_cli_unsupported_language_exits_2(self):
        code, _, err = run(['--lang', 'uk', 'Your money'])
        self.assertEqual(code, 2)
        self.assertIn('Ukrainian', err)
        code, _, err = run(['--lang', 'xx', 'foo'])
        self.assertEqual(code, 2)
        self.assertIn('not supported', err)

    def test_file_level_unsupported_language_exits_2_with_position(self):
        p = self.write('a.html', '<html lang="uk">\n<body><p>Привіт</p></body></html>')
        code, _, err = run([p])
        self.assertEqual(code, 2)
        self.assertIn('Ukrainian', err)
        self.assertIn('a.html:2:', err)

    def test_en_us_and_en_gb_use_their_own_dictionary(self):
        code, _, _ = run(['--lang', 'en-US', '--no-languagetool', 'the color'])
        self.assertEqual(code, 0)
        code, out, _ = run(['--lang', 'en-GB', '--no-languagetool', 'the color'])
        self.assertEqual(code, 1)
        self.assertIn('"color"', out)
        code, out, _ = run(['--lang', 'en-US', '--no-languagetool', 'the colour'])
        self.assertEqual(code, 1)
        self.assertIn('"colour"', out)

    def test_languagetool_gets_the_variant(self):
        for tag, expected in (('en-US', 'en-US'), ('en', 'en-GB'), ('de-AT', 'de-AT'), ('de-CH', 'de-CH'),
                              ('de', 'de-DE')):
            opener = fake_urlopen()
            run(['--lang', tag, 'Your money'], opener=opener)
            self.assertEqual(opener.calls[0][1]['language'], expected, tag)

    def test_german_variants_load_their_own_dictionary(self):
        for name in ('de_AT_frami', 'de_CH_frami'):
            shutil.copy(os.path.join(FIXTURE_DICTS, 'en_GB.aff'), os.path.join(self.tmp.name, name + '.aff'))
        with open(os.path.join(self.tmp.name, 'de_AT_frami.dic'), 'w', encoding='utf-8') as f:
            f.write('2\nJänner\ngut\n')
        with open(os.path.join(self.tmp.name, 'de_CH_frami.dic'), 'w', encoding='utf-8') as f:
            f.write('2\ngross\ngut\n')
        self.assertEqual(run(['--lang', 'de-AT', '--no-languagetool', 'Jänner gut'], dicts=self.tmp.name)[0], 0)
        self.assertEqual(run(['--lang', 'de-CH', '--no-languagetool', 'gross gut'], dicts=self.tmp.name)[0], 0)
        code, out, _ = run(['--lang', 'de-CH', '--no-languagetool', 'Jänner'], dicts=self.tmp.name)
        self.assertEqual(code, 1)
        code, out, _ = run(['--lang', 'de', '--no-languagetool', 'gut'], dicts=self.tmp.name)
        self.assertEqual(code, 3)                      # no de_DE_frami here: not silently de-AT
        self.assertIn('fetch_dicts.py de-DE', out)

    def test_fetch_dicts_knows_every_variant_at_the_pinned_commit(self):
        self.assertEqual(fetch_dicts.SOURCES['de-AT'], 'de/de_AT_frami')
        self.assertEqual(fetch_dicts.SOURCES['de-CH'], 'de/de_CH_frami')
        self.assertEqual(fetch_dicts.SOURCES['en-US'], 'en/en_US')
        self.assertEqual(set(fetch_dicts.SOURCES), set(langcheck.LANGS))
        urls = []

        def download(url, dst):
            urls.append(url)
            open(dst, 'w').close()
        with mock.patch.dict(os.environ, {'LANGTOOLS_DICTS': self.tmp.name}), \
                mock.patch.object(fetch_dicts, 'download', download), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fetch_dicts.main(['en-US', 'de_AT']), 0)
        self.assertEqual(urls, [fetch_dicts.BASE.format(ref=fetch_dicts.REF) + p for p in
                                ('en/en_US.aff', 'en/en_US.dic', 'de/de_AT_frami.aff', 'de/de_AT_frami.dic')])

    def test_fetch_dicts_rejects_country_codes(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(fetch_dicts.main(['cz']), 2)
        self.assertIn('cs', err.getvalue())


# ------------------------------------------------------------ 3: capitals, 10: domains

class WordsTest(TempFiles):
    def test_all_caps_words_are_checked(self):
        code, rep, _ = run_json(['--lang', 'cs', '--no-languagetool', 'STÁHNOUT ZDRAMA'])
        self.assertEqual(code, 1)
        self.assertEqual([f['found'] for f in rep['findings']], ['ZDRAMA'])

    def test_all_caps_proper_noun_and_clean_caps(self):
        self.assertEqual(run(['--lang', 'cs', '--no-languagetool', 'STÁHNOUT ZDARMA PRAHA'])[0], 0)

    def test_acronym_rule(self):
        # skipped: digits in a short token, or listed (API, SMS are in the default list)
        self.assertEqual(run(['--lang', 'en', '--no-languagetool', 'get B2B API SMS 4K in 2026'])[0], 0)
        # a short capital word that is neither is checked like any other
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', 'get XQZW'])
        self.assertEqual([f['found'] for f in rep['findings']], ['XQZW'])

    def test_domains_need_a_known_tld_or_scheme(self):
        ws = [w for w, _ in langcheck.words('vis.zkus www.example.cz info@example.com https://x.y/z example.com done.')]
        self.assertEqual(ws, ['vis', 'zkus', 'done'])

    def test_missing_space_after_full_stop_is_still_checked(self):
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', 'Your mony.Get a friend'])
        self.assertEqual(code, 1)
        self.assertEqual([f['found'] for f in rep['findings']], ['mony'])

    def test_word_offsets(self):
        self.assertEqual(list(langcheck.words('Ahoj, svete')), [('Ahoj', 0), ('svete', 6)])


# ------------------------------------------------------------ 4, 5: HTML

class HtmlTest(TempFiles):
    def snippets(self, html_text):
        return langcheck.parse_html(html_text, 'p.html')

    def test_inline_elements_join_the_sentence(self):
        s = self.snippets('<p>Pozvi <a href="#">kamaráda</a> a<br>získej <span>odměnu</span>.</p>'
                          '<p>Druhý <b>odstavec</b></p>')
        self.assertEqual(texts_of(s), ['Pozvi kamaráda a získej odměnu.', 'Druhý odstavec'])

    def test_block_elements_split(self):
        s = self.snippets('<div>Jedna</div><div>Dva<ul><li>Tři</li><li>Čtyři</li></ul></div>')
        self.assertEqual(texts_of(s), ['Jedna', 'Dva', 'Tři', 'Čtyři'])

    def test_head_texts_are_checked(self):
        s = self.snippets('<html lang="sk"><head><title>Titulok stránky</title>'
                          '<meta name="description" content="Popis stránky">'
                          '<meta property="og:title" content="OG titulok">'
                          '<meta property="og:description" content="OG popis">'
                          '<meta name="viewport" content="width=device-width">'
                          '<style>p{}</style><script>var x;</script></head>'
                          '<body><h1>Ahoj</h1><img alt="Obrázok"><button aria-label="Zavrieť">x</button>'
                          '<p>Text <b>tučne</b></p><noscript>nie</noscript></body></html>')
        self.assertEqual(texts_of(s), ['Titulok stránky', 'Popis stránky', 'OG titulok', 'OG popis', 'Ahoj',
                                       'Obrázok', 'Zavrieť', 'x', 'Text tučne'])
        self.assertTrue(all(x.lang == 'sk' for x in s))

    def test_element_lang_for_mixed_pages(self):
        s = self.snippets('<html lang="cs"><body><p>Pozvi kamaráda <span lang="en">and get</span> odměnu</p>'
                          '<blockquote lang="de-AT"><p>Grüß Gott</p></blockquote><p>Konec</p></body></html>')
        self.assertEqual([(x.text, x.lang) for x in s], [('Pozvi kamaráda', 'cs'), ('and get', 'en'),
                                                       ('odměnu', 'cs'), ('Grüß Gott', 'de-AT'), ('Konec', 'cs')])

    def test_mixed_page_is_checked_per_language(self):
        p = self.write('mix.html', '<html lang="cs"><body><p>Pozvi kamaráda</p>'
                                   '<p lang="en">Invite a frend</p></body></html>')
        code, rep, _ = run_json(['--no-languagetool', p])
        self.assertEqual(code, 1)
        self.assertEqual(set(rep['checked']), {'cs-CZ', 'en-GB'})
        self.assertEqual([f['found'] for f in rep['findings']], ['frend'])

    def test_unsupported_element_language_is_not_checked(self):
        p = self.write('fr.html', '<html lang="cs"><body><p>Pozvi kamaráda</p>'
                                  '<q lang="fr">Bonjour tout le monde</q></body></html>')
        code, out, _ = run(['--no-languagetool', p])
        self.assertEqual(code, 3)
        self.assertIn('lang="fr"', out)
        code, out, _ = run(['--no-languagetool', '--lenient', p])
        self.assertEqual(code, 0)
        self.assertIn('not supported', out)


# ------------------------------------------------------------ 6: positions

class PositionTest(TempFiles):
    def test_text_output_has_file_line_col(self):
        p = self.write('copy.md', '# Title\n\nYour money in one place.\nInvite a frend\n')
        code, out, _ = run(['--lang', 'en', '--no-languagetool', p])
        self.assertEqual(code, 1)
        self.assertIn(f'{p}:4:10: [hunspell] "frend"', out)

    def test_json_positions(self):
        p = self.write('ads.json', '{\n  "lang": "en",\n  "headline": "Get a frend",\n  "cta": ["Your mony"]\n}\n')
        code, rep, _ = run_json(['--no-languagetool', p])
        self.assertEqual([(f['found'], f['line'], f['col'], f['offset']) for f in rep['findings']],
                         [('frend', 3, 22, 6), ('mony', 4, 17, 5)])

    def test_html_positions(self):
        p = self.write('p.html', '<html lang="en">\n<body>\n  <p>Your money\n     and a <a href="#">frend</a></p>\n'
                                 '  <img alt="mony">\n</body></html>')
        code, rep, _ = run_json(['--no-languagetool', p])
        self.assertEqual([(f['found'], f['line'], f['col']) for f in rep['findings']],
                         [('frend', 4, 24), ('mony', 5, 13)])

    def test_repeated_word_gets_each_position(self):
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', 'frend and frend'])
        self.assertEqual([(f['line'], f['col']) for f in rep['findings']], [(1, 1), (1, 11)])

    def test_same_text_in_two_files_is_reported_for_both(self):
        a = self.write('a.txt', 'Get a frend')
        b = self.write('b.txt', '\n\nGet a frend')
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', a, b])
        self.assertEqual([(f['source'], f['line']) for f in rep['findings']], [(a, 1), (b, 3)])

    def test_multiword_finding_across_a_line_break(self):
        s = langcheck.parse_text('Das ist ein\ngute Zeichen', 'n.md')[0]
        self.assertEqual(s.locate(8, 'ein gute Zeichen'), (1, 9))
        self.assertEqual(s.locate(12, 'gute'), (2, 1))


# ------------------------------------------------------------ 7: --lang is a fallback

class LangPrecedenceTest(TempFiles):
    def test_file_language_wins_over_lang(self):
        p = self.write('sk.html', '<html lang="sk"><body><p>Ahoj</p></body></html>')
        code, rep, _ = run_json(['--lang', 'cs', '--no-languagetool', p, 'Pozvi kamaráda'])
        self.assertEqual(set(rep['checked']), {'sk-SK', 'cs-CZ'})
        self.assertEqual(rep['checked']['sk-SK']['texts'], 1)

    def test_lang_fills_in_where_the_file_says_nothing(self):
        p = self.write('plain.txt', 'Your money')
        code, rep, _ = run_json(['--lang', 'en-US', '--no-languagetool', p])
        self.assertEqual(list(rep['checked']), ['en-US'])

    def test_force_lang_overrides(self):
        p = self.write('sk.html', '<html lang="sk"><body><p>Pozvi kamaráda</p></body></html>')
        code, rep, _ = run_json(['--force-lang', 'cs', '--no-languagetool', p])
        self.assertEqual(code, 0)
        self.assertEqual(list(rep['checked']), ['cs-CZ'])

    def test_force_lang_also_covers_unsupported_file_languages(self):
        p = self.write('x.json', json.dumps({'lang': 'uk', 'text': 'Your money'}))
        self.assertEqual(run([p])[0], 2)
        self.assertEqual(run(['--force-lang', 'en', '--no-languagetool', p])[0], 0)


# ------------------------------------------------------------ 8: strict by default, retries

class DegradeTest(TempFiles):
    def test_missing_languagetool_is_exit_3_by_default(self):
        code, out, _ = run(['--lang', 'en', 'Your money'])
        self.assertEqual(code, 3)
        self.assertIn('grammar NOT checked', out)
        self.assertIn('NOT CHECKED', out)

    def test_lenient_accepts_a_missing_layer(self):
        code, out, _ = run(['--lang', 'en', '--lenient', 'Your money'])
        self.assertEqual(code, 0)
        self.assertIn('[languagetool]', out)

    def test_strict_flag_still_accepted(self):
        self.assertEqual(run(['--lang', 'en', '--strict', 'Your money'])[0], 3)
        self.assertEqual(run(['--strict', '--lenient', 'x'])[0], 2)

    def test_no_layer_at_all_is_exit_3_even_when_lenient(self):
        code, out, _ = run(['--lang', 'de', '--lenient', 'Alles gut'], dicts=self.tmp.name)
        self.assertEqual(code, 3)
        self.assertIn('NOT CHECKED', out)

    def test_unchecked_wins_over_findings_when_strict(self):
        self.assertEqual(run(['--lang', 'en', 'Your mony'])[0], 3)            # since 1.3.0, findings still listed
        self.assertEqual(run(['--lang', 'en', '--lenient', 'Your mony'])[0], 1)

    def test_one_failure_is_retried(self):
        def flaky(n, url, form):
            if n == 1:
                raise urllib.error.HTTPError(url, 502, 'Bad Gateway', {}, None)
        opener = fake_urlopen(script=flaky)
        code, rep, _ = run_json(['--lang', 'en', 'Your money'], opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(rep['checked']['en-GB']['ran'], ['hunspell', 'languagetool'])

    def test_failure_mid_run_keeps_earlier_findings_and_marks_the_rest(self):
        texts = ['Your money', 'Get a friend', 'In one place']

        def dies_after_first(n, url, form):
            if form['text'] != texts[0]:
                raise urllib.error.HTTPError(url, 500, 'Server Error', {}, None)
        opener = fake_urlopen(lt_matches={texts[0]: [lt_match(0, 4, repl=['You'])]}, script=dies_after_first)
        p = self.write('t.txt', '\n\n'.join(texts))
        code, rep, _ = run_json(['--lang', 'en', p], opener=opener)
        self.assertEqual(code, 3)                                    # not fully checked, the finding is kept
        self.assertEqual([f['tool'] for f in rep['findings']], ['languagetool'])
        sent = sorted(form['text'] for _, form in opener.calls)     # sent concurrently, order varies
        self.assertEqual(sent, sorted([texts[0], texts[1], texts[1], texts[2], texts[2]]))   # each retried once
        self.assertIn('2 of 3 texts not checked', rep['checked']['en-GB']['skipped']['languagetool'])
        self.assertEqual(rep['checked']['en-GB']['partly'], ['languagetool'])

    def test_failure_mid_run_without_findings_is_exit_3(self):
        def dies_after_first(n, url, form):
            if n > 1:
                raise REFUSED
        opener = fake_urlopen(script=dies_after_first)
        p = self.write('t.txt', 'Your money\n\nGet a friend\n\nIn one place')
        code, out, _ = run(['--lang', 'en', p], opener=opener)
        self.assertEqual(code, 3)
        self.assertLessEqual(len(opener.calls), 5)   # server gone: retried once, only requests already in flight
        self.assertIn('languagetool (partly)', out)

    def test_http_error_is_reported(self):
        err = urllib.error.HTTPError('http://lt.test', 429, 'Too Many Requests', {}, None)
        code, out, _ = run(['--lang', 'en', 'Your money'], opener=fake_urlopen(fail=err))
        self.assertEqual(code, 3)
        self.assertIn('HTTP 429', out)


# ------------------------------------------------------------ 9: merged findings, rule config

class LanguageToolTest(TempFiles):
    def test_match_becomes_finding_with_rule(self):
        opener = fake_urlopen(lt_matches={'Your money': [lt_match(0, 4, repl=['You', 'Yours'])]})
        code, rep, _ = run_json(['--lang', 'en', 'Your money'], opener=opener)
        self.assertEqual(code, 1)
        lt = [f for f in rep['findings'] if f['tool'] == 'languagetool']
        self.assertEqual((lt[0]['rule'], lt[0]['suggest']), ('AGREEMENT', ['You', 'Yours']))
        self.assertEqual(rep['checked']['en-GB']['ran'], ['hunspell', 'languagetool'])
        url, form = opener.calls[0]
        self.assertEqual(url, 'http://lt.test/v2/check')
        self.assertIn('WHITESPACE_RULE', form['disabledRules'])

    def test_same_span_from_both_layers_is_one_finding(self):
        opener = fake_urlopen(lt_matches={'Get a frend': [
            lt_match(6, 5, rule='MORFOLOGIK_RULE_EN_GB', message='Possible spelling', repl=['friend', 'fend'])]})
        code, rep, _ = run_json(['--lang', 'en', 'Get a frend'], opener=opener)
        self.assertEqual(code, 1)
        self.assertEqual(len(rep['findings']), 1)
        f = rep['findings'][0]
        self.assertEqual((f['tool'], f['also']), ('hunspell', ['languagetool MORFOLOGIK_RULE_EN_GB']))
        self.assertIn('fend', f['suggest'])
        code, out, _ = run(['--lang', 'en', 'Get a frend'], opener=opener)
        self.assertIn('[hunspell +languagetool MORFOLOGIK_RULE_EN_GB] "frend"', out)

    def test_different_spans_are_kept_apart(self):
        opener = fake_urlopen(lt_matches={'Get a frend': [lt_match(0, 3, rule='X')]})
        code, rep, _ = run_json(['--lang', 'en', 'Get a frend'], opener=opener)
        self.assertEqual(sorted(f['tool'] for f in rep['findings']), ['hunspell', 'languagetool'])

    def test_disable_rule_from_cli_and_ignore_file(self):
        opener = fake_urlopen(lt_matches={'Your money': [lt_match(0, 4, rule='MY_RULE'), lt_match(5, 5, rule='OTHER')]})
        words = self.write('words.txt', 'rule:OTHER\n')
        code, rep, _ = run_json(['--lang', 'en', '--disable-rule', 'MY_RULE', '--ignore', words, 'Your money'],
                                opener=opener)
        self.assertEqual(code, 0)                      # filtered locally too, whatever the server does
        rules = opener.calls[0][1]['disabledRules'].split(',')
        self.assertIn('MY_RULE', rules)
        self.assertIn('OTHER', rules)

    def test_czech_never_goes_to_languagetool(self):
        opener = fake_urlopen()
        self.assertEqual(run(['--lang', 'cs', 'Pozvi kamaráda'], opener=opener)[0], 0)
        self.assertEqual(opener.calls, [])


# ------------------------------------------------------------ 11: Korektor

class KorektorTest(TempFiles):
    def test_off_by_default(self):
        opener = fake_urlopen()
        self.assertEqual(run(['--lang', 'cs', 'Pozvi kamaráda'], opener=opener)[0], 0)
        self.assertEqual(opener.calls, [])

    def test_alignment_by_offsets(self):
        f = langcheck.korektor_diff('Pozvi kamarada a ziskej odmenu', 'Pozvi kamaráda a získej odměnu', lambda w: False)
        self.assertEqual([(x.found, x.offset, x.suggest) for x in f],
                         [('kamarada', 6, ['kamaráda']), ('ziskej', 17, ['získej']), ('odmenu', 24, ['odměnu'])])

    def test_alignment_survives_a_different_token_count(self):
        """The old version gave up and flagged the whole text when the counts differed."""
        f = langcheck.korektor_diff('Pozvi kamarada a ziskej odmenu', 'Pozvi kamaráda a taky získej odměnu',
                                    lambda w: False)
        self.assertEqual([x.found for x in f][0], 'kamarada')
        self.assertIn('odmenu', [x.found for x in f])
        self.assertTrue(all(len(x.found) < 20 for x in f))
        f = langcheck.korektor_diff('Ahoj, jak se mas mas', 'Ahoj, jak se máš', lambda w: False)
        self.assertEqual([(x.found, x.offset) for x in f], [('mas mas', 13)])

    def test_punctuation_is_not_part_of_the_word(self):
        f = langcheck.korektor_diff('Pozvi kamarada, hned!', 'Pozvi kamaráda, hned!', lambda w: False)
        self.assertEqual([(x.found, x.offset) for x in f], [('kamarada', 6)])

    def test_batched_into_one_request(self):
        opener = fake_urlopen(korektor={'Pozvi kamarada': 'Pozvi kamaráda'})
        p = self.write('cs.txt', 'Pozvi kamarada\n\nZískej odměnu\n\nPozvi kamaráda')
        code, rep, _ = run_json(['--lang', 'cs', '--online', p], opener=opener)
        self.assertEqual(code, 1)
        self.assertEqual(len(opener.calls), 1)
        url, form = opener.calls[0]
        self.assertEqual(url, langcheck.KOREKTOR_URL)
        self.assertEqual(form['data'], 'Pozvi kamarada\n\nZískej odměnu\n\nPozvi kamaráda')   # POST body, not URL
        # Hunspell flags the same word, so it is one finding with Korektor listed under `also`
        self.assertEqual([(f['found'], f['tool'], f['also'], f['line']) for f in rep['findings']],
                         [('kamarada', 'hunspell', ['korektor'], 1)])
        self.assertIn('kamaráda', rep['findings'][0]['suggest'])

    def test_batch_line_mismatch_falls_back_to_one_by_one(self):
        def merge_lines(n, url, form):
            if '\n' in form['data']:
                raise _Return(json.dumps({'result': 'one line only'}))
        opener = _returning(fake_urlopen(script=merge_lines))
        p = self.write('cs.txt', 'Pozvi kamaráda\n\nZískej odměnu')
        code, rep, _ = run_json(['--lang', 'cs', '--online', p], opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual([form['data'] for _, form in opener.calls],
                         ['Pozvi kamaráda\n\nZískej odměnu', 'Pozvi kamaráda', 'Získej odměnu'])

    def test_batch_keeps_texts_apart(self):
        """Joined by a single newline, the real service read a headline without a full stop and the
        next text as one sentence and lowercased "Přehled" (public API, 2026-10-13). A blank line
        between texts keeps them apart; this fake behaves like the service did."""
        def like_the_service(n, url, form):
            fixed = re.sub(r'(?<!\n)\n(?!\n)(\w)', lambda m: '\n' + m.group(1).lower(), form['data'])
            raise _Return(json.dumps({'result': fixed}))
        opener = _returning(fake_urlopen(script=like_the_service))
        p = self.write('cs.txt', 'Pozvi kamaráda\n\nPřehled o penězích')
        code, rep, _ = run_json(['--lang', 'cs', '--online', p], opener=opener)
        self.assertEqual([f['found'] for f in rep['findings'] if 'korektor' in [f['tool'], *f['also']]], [])

    def test_unreachable_falls_back_to_dictionary(self):
        code, out, _ = run(['--lang', 'cs', '--online', 'Pozvi kamaráda'])
        self.assertEqual(code, 3)
        self.assertIn('Czech checked by dictionary only', out)
        self.assertEqual(run(['--lang', 'cs', '--online', '--lenient', 'Pozvi kamaráda'])[0], 0)

    def test_no_online_is_deprecated_but_still_off(self):
        opener = fake_urlopen()
        code, _, err = run(['--lang', 'cs', '--online', '--no-online', 'Pozvi kamaráda'], opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(opener.calls, [])
        self.assertIn('deprecated', err)


class _Return(Exception):
    """Raised by a script to make the fake server answer with this body instead."""


def _returning(opener):
    def _open(url, data=None, timeout=None):
        try:
            return opener(url, data, timeout)
        except _Return as r:
            return FakeResponse(str(r).encode())
    _open.calls = opener.calls
    return _open


# ------------------------------------------------------------ 12: ignore list, JSON, action

class IgnoreTest(TempFiles):
    def test_case_sensitive_by_default(self):
        ign = langcheck.IgnoreList(['Zorbly', 'fintech'])
        self.assertTrue(ign('Zorbly'))
        self.assertTrue(ign('ZORBLY'))
        self.assertFalse(ign('zorbly'))                 # a brand written wrong is a finding
        self.assertTrue(ign('Fintech'))                 # lowercase entry: sentence start is fine
        self.assertFalse(ign('FinTech'))

    def test_i_prefix_is_case_insensitive(self):
        ign = langcheck.IgnoreList(['i:zorbly', '# comment', ''])
        self.assertTrue(ign('zorbly') and ign('Zorbly') and ign('ZoRbLy'))
        self.assertFalse(ign('# comment'))

    def test_ignore_file_on_the_cli(self):
        words = self.write('words.txt', '# brand\nZorbly\n')
        self.assertEqual(run(['--lang', 'en', '--no-languagetool', '--ignore', words, 'Get Zorbly on iOS'])[0], 0)
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '--ignore', words, 'Get zorbly'])
        self.assertEqual(code, 1)
        self.assertIn('"zorbly"', out)

    def test_defaults_carry_no_author_words(self):
        self.assertNotIn('appka', langcheck.DEFAULT_IGNORE)
        example = os.path.join(ROOT, 'examples', 'ignore.example.txt')
        ign = langcheck.make_ignored([example], defaults=())
        self.assertTrue(ign('appka'))
        self.assertEqual(ign.rules, ['EN_COMPOUNDS'])


class InputTest(TempFiles):
    def test_markdown_strips_markup(self):
        md = '# Title\n\nSee [the app](https://x.y) and `code`.\n\n```\nskip me\n```\n\n- **Bold** item'
        self.assertEqual(texts_of(langcheck.parse_text(md)),
                         ['Title', f'See the app and {langcheck.PLACEHOLDER}.', 'Bold item'])

    def test_json_known_keys_and_lang(self):
        p = self.write('a.json', json.dumps({'lang': 'pl', 'headline': 'H', 'copy': ['A', 'B'], 'url': 'skip',
                                             'nested': {'cta': 'C'}}))
        s = langcheck.collect(p)
        self.assertEqual([(x.text, x.lang) for x in s], [('H', 'pl'), ('A', 'pl'), ('B', 'pl'), ('C', 'pl')])

    def test_json_per_item_lang(self):
        p = self.write('ads.json', json.dumps({'ads': [{'lang': 'sk', 'headline': 'Ahoj'},
                                                       {'lang': 'de-AT', 'headline': 'Servus'},
                                                       {'headline': 'Your money'}]}))
        code, rep, _ = run_json(['--lang', 'en', '--no-languagetool', '--lenient', p])
        self.assertEqual({k: v['texts'] for k, v in rep['checked'].items()}, {'sk-SK': 1, 'de-AT': 1, 'en-GB': 1})

    def test_json_market_is_not_a_language(self):
        p = self.write('m.json', json.dumps({'market': 'CZ', 'headline': 'Pozvi kamaráda'}))
        code, _, err = run([p])
        self.assertEqual(code, 2)
        self.assertIn('--lang', err)

    def test_json_all_strings_skips_lang_value(self):
        p = self.write('a.json', json.dumps({'lang': 'pl', 'k': 'one', 'list': ['two']}))
        self.assertEqual(texts_of(langcheck.collect(p, all_strings=True)), ['one', 'two'])

    def test_json_with_bom(self):
        p = os.path.join(self.tmp.name, 'bom.json')
        with open(p, 'w', encoding='utf-8-sig') as f:
            f.write('{"lang": "en", "text": "hi"}')
        self.assertEqual([(x.text, x.lang, x.line) for x in langcheck.collect(p)], [('hi', 'en', 1)])


class ActionTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'action.yml'), encoding='utf-8') as f:
            self.action = f.read()

    def test_python_is_only_set_up_when_missing(self):
        block = self.action.split('uses: actions/setup-python')[0].rsplit('- ', 1)[1]
        self.assertIn("if: steps.python.outputs.found != 'true'", block)
        self.assertIn('--target', self.action)        # spylls goes to a private folder, not the user's site-packages

    def test_strict_default_and_pinned_image(self):
        self.assertRegex(self.action, r"strict:\n(?:    .*\n)*?    default: 'true'")
        with open(os.path.join(ROOT, 'scripts', 'languagetool.sh'), encoding='utf-8') as f:
            self.assertRegex(f.read(), r'erikvl87/languagetool:6\.8@sha256:[0-9a-f]{64}')
        self.assertIn('RUNNER_OS', self.action)

    def test_selftest_has_a_grammar_only_case(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'selftest.yml'), encoding='utf-8') as f:
            wf = f.read()
        self.assertIn('examples/grammar-de.txt', wf)
        self.assertIn("'languagetool'", wf)


# ------------------------------------------------------------ CLI basics

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
        self.assertIn('<arg1>:1:6: [hunspell] "mony" -> money', out)

    def test_missing_lang_is_usage_error(self):
        code, _, err = run(['Some text'])
        self.assertEqual(code, 2)
        self.assertIn('--lang', err)

    def test_lang_without_value_is_usage_error(self):
        self.assertEqual(run(['--lang'])[0], 2)

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
        code, _, err = run([self.write('strings.json', json.dumps({'lang': 'en', 'welcome_msg': 'Hi'}))])
        self.assertEqual(code, 2)
        self.assertIn('--all-strings', err)

    def test_stdin(self):
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '-'], stdin='Get a frend\n\nYour money')
        self.assertEqual(code, 1)
        self.assertIn('<stdin>:1:7:', out)

    def test_json_out_file(self):
        target = os.path.join(self.tmp.name, 'report.json')
        code, out, _ = run(['--lang', 'en', '--no-languagetool', '--json-out', target, 'Get a frend'])
        self.assertEqual(code, 1)
        self.assertIn('findings', out)
        with open(target, encoding='utf-8') as f:
            rep = json.load(f)
        self.assertEqual((rep['exit_code'], rep['version']), (1, langcheck.__version__))


EXAMPLE_DICTS = ('cs_CZ', 'sk_SK', 'pl_PL', 'de_DE_frami')


@unittest.skipUnless(all(os.path.exists(os.path.join(REAL_DICTS, n + '.dic')) for n in EXAMPLE_DICTS),
                     'run python fetch_dicts.py first')
class ExamplesTest(TempFiles):
    """The bundled examples with the real LibreOffice dictionaries, network layers off."""

    def hunspell_words(self, *argv):
        code, rep, _ = run_json(['--no-languagetool', '--no-suggest', *argv], dicts=REAL_DICTS)
        return code, [f['found'] for f in rep['findings']]

    def test_planted_mistakes_are_caught(self):
        ex = os.path.join(ROOT, 'examples')
        self.assertEqual(self.hunspell_words(os.path.join(ex, 'ad-copy.json')), (1, ['kamarada', 'ziskej', 'odmenu']))
        self.assertEqual(self.hunspell_words(os.path.join(ex, 'landing-pl.html')), (1, ['nagrode', 'Sprawdz']))
        self.assertEqual(self.hunspell_words('--lang', 'de', os.path.join(ex, 'newsletter-de.md')), (1, ['Weihnachen']))

    def test_clean_and_grammar_only_examples_pass_the_dictionary(self):
        ex = os.path.join(ROOT, 'examples')
        self.assertEqual(self.hunspell_words('--lang', 'sk', os.path.join(ex, 'clean-sk.txt')), (0, []))
        # The selftest relies on this: only LanguageTool can catch the mistake in this file.
        self.assertEqual(self.hunspell_words('--lang', 'de', os.path.join(ex, 'grammar-de.txt')), (0, []))


if __name__ == '__main__':
    unittest.main()
