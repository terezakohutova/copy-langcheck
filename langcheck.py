"""Spelling and grammar check for marketing copy in Czech, Slovak, Polish, German and English.

Read-only: it never changes your text. Exit code 1 when anything is found, so it
can gate a build, a commit or an ad upload.

    python langcheck.py --lang cs "Pozvi kamaráda a získej odměnu"
    python langcheck.py --lang pl copy.txt              # plain text or Markdown, one check per paragraph
    python langcheck.py page.html ads.json              # several files; HTML/JSON can state their own language
    python langcheck.py --lang de --all-strings app-strings.json   # every string value in the JSON
    echo "Text" | python langcheck.py --lang en -       # read from stdin

Layers, each reported when it cannot run:
  1. Hunspell via spylls + LibreOffice dictionaries, fully local (python fetch_dicts.py).
  2. LanguageTool, a server you run (LT_URL, default http://localhost:8081/v2/check).
     Grammar, agreement, punctuation for sk, pl, de, en. It has no Czech module.
  3. Korektor (UFAL, Charles University) for Czech, via the public LINDAT API. Off
     unless you pass --online: it sends text out, and its models are CC BY-NC-SA
     (commercial use needs the authors' written permission).

Exit codes: 0 clean, 1 findings, 2 bad input, 3 could not check (no layer ran for a
language, or with --strict: any layer that applies did not run).
"""
from __future__ import annotations

import argparse
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

__version__ = '1.1.0'

HERE = os.path.dirname(os.path.abspath(__file__))
KOREKTOR_URL = 'https://lindat.mff.cuni.cz/services/korektor/api/correct'
KOREKTOR_MODEL = 'czech-spellchecker-130202'

EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, EXIT_UNCHECKED = 0, 1, 2, 3

SUPPORTED = ('cs', 'sk', 'pl', 'de', 'en')
ALIASES = {'cz': 'cs', 'cs-cz': 'cs', 'sk-sk': 'sk', 'pl-pl': 'pl', 'de-de': 'de', 'at': 'de',
           'de-at': 'de', 'ch': 'de', 'de-ch': 'de', 'en-gb': 'en', 'en-us': 'en', 'uk': 'en', 'gb': 'en'}
LT_LANG = {'en': 'en-GB', 'de': 'de-DE', 'pl': 'pl-PL', 'sk': 'sk-SK'}
HUNSPELL = {'cs': 'cs_CZ', 'sk': 'sk_SK', 'pl': 'pl_PL', 'de': 'de_DE_frami', 'en': 'en_GB'}
FILE_EXTS = ('.txt', '.md', '.markdown', '.html', '.htm', '.json')

# Platform, tech and currency words that are never typos in marketing copy.
DEFAULT_IGNORE = frozenset({
    'iOS', 'Android', 'App', 'Store', 'Google', 'Play', 'Apple', 'Pay', 'Meta', 'TikTok', 'Instagram',
    'Facebook', 'YouTube', 'WhatsApp', 'LinkedIn', 'Visa', 'Mastercard', 'PayPal', 'API', 'CTA',
    'EUR', 'USD', 'CZK', 'PLN', 'Kč', 'zł', 'app', 'apps', 'appka', 'appku', 'appce', 'appky',
    'cashback', 'online', 'e-mail', 'email', 'newsletter', 'onboarding', 'fintech', 'premium', 'www',
})

# Picky-level LanguageTool rules that flag copy formatting (headlines without a full
# stop, typographic quotes, line fragments), not the language itself.
LT_DISABLED = ','.join([
    'WHITESPACE_RULE', 'PUNCTUATION_PARAGRAPH_END', 'UPPERCASE_SENTENCE_START', 'EN_QUOTES',
    'DASH_RULE', 'TYPOGRAFISCHE_ANFUEHRUNGSZEICHEN', 'APOS_TYP', 'TYPOGRAPHY', 'UNPAIRED_BRACKETS',
    'DOUBLE_PUNCTUATION', 'SENTENCE_WHITESPACE', 'PUNKT_ENDE_ABSATZ', 'EN_UNPAIRED_QUOTES',
    'PL_QUOTES', 'SK_QUOTES', 'ELLIPSIS', 'BRAK_KROPKI', 'TYPOGRAFISCHE_APOSTROPHE',
])
TEXT_KEYS = {'headline', 'headlines', 'title', 'subtitle', 'sub', 'subline', 'cta', 'text', 'copy',
             'body', 'primary_text', 'description', 'descriptions', 'long_headline', 'subject', 'preheader'}
SKIP_TAGS = {'script', 'style', 'svg', 'template', 'noscript', 'code', 'pre', 'head'}
BLOCK_TAGS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'div', 'section', 'button', 'label', 'td',
              'th', 'a', 'span', 'figcaption', 'blockquote', 'summary', 'option', 'br'}

Ignored = Callable[[str], bool]


class UsageError(Exception):
    """Bad input: unknown language, missing file, unreadable JSON. Exit code 2."""


@dataclass
class Finding:
    tool: str
    found: str
    suggest: list[str]
    message: str
    text: str
    rule: str = ''
    source: str = ''


@dataclass
class LayerResult:
    findings: list[Finding] = field(default_factory=list)
    error: str = ''          # empty = the layer ran on every text


def dicts_dir() -> str:
    return os.environ.get('LANGTOOLS_DICTS') or os.path.join(HERE, 'dicts')


def lt_url() -> str:
    return os.environ.get('LT_URL') or 'http://localhost:8081/v2/check'


def norm_lang(x: str | None) -> str:
    x = (x or '').strip().lower().replace('_', '-')
    return ALIASES.get(x, x.split('-')[0] if x else '')


def make_ignored(paths: Iterable[str]) -> Ignored:
    """Return a predicate for words that are right on purpose (case-insensitive)."""
    words = set(DEFAULT_IGNORE)
    for p in paths:
        with open(p, encoding='utf-8') as f:
            words.update(w.strip() for w in f if w.strip() and not w.lstrip().startswith('#'))
    lower = {w.lower() for w in words}
    return lambda w: w.lower() in lower


# ---------------------------------------------------------------- input

class _HTML(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ''
        self.out: list[str] = []
        self.buf: list[str] = []
        self.skip = 0

    def flush(self) -> None:
        t = re.sub(r'\s+', ' ', ''.join(self.buf)).strip()
        if t:
            self.out.append(t)
        self.buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == 'html' and a.get('lang'):
            self.lang = a['lang'] or ''
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.flush()
        for k in ('alt', 'title', 'placeholder', 'aria-label'):
            v = a.get(k)
            if v and v.strip() and not self.skip:
                self.out.append(v.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)


def parse_text(raw: str) -> list[str]:
    """Plain text or Markdown: one text per paragraph, markup stripped."""
    text = re.sub(r'```.*?```', ' ', raw, flags=re.S)                    # code blocks
    text = re.sub(r'`[^`]*`|!\[[^\]]*\]\([^)]*\)|<[^>]+>', ' ', text)    # inline code, images, tags
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)                  # links -> their text
    text = re.sub(r'^[ \t]{0,3}(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+|>[ \t]?)', '', text, flags=re.M)
    text = text.replace('**', '').replace('__', '')
    return [re.sub(r'\s+', ' ', p).strip() for p in re.split(r'\n\s*\n', text) if p.strip()]


def collect(path: str, all_strings: bool = False) -> tuple[str, list[str]]:
    """Return (language stated in the file or '', [texts]) for a .json, .html or text/Markdown file."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding='utf-8-sig') as f:
        raw = f.read()
    if ext == '.json':
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UsageError(f'{path}: not valid JSON ({e})') from None
        texts: list[str] = []

        def walk(x: Any, key: str = '') -> None:
            if isinstance(x, dict):
                for k, v in x.items():
                    if k not in ('lang', 'language', 'market'):
                        walk(v, k)
            elif isinstance(x, list):
                for v in x:
                    walk(v, key)
            elif isinstance(x, str) and (all_strings or key in TEXT_KEYS):
                texts.append(x)
        walk(d)
        lang = (d.get('lang') or d.get('language') or d.get('market') or '') if isinstance(d, dict) else ''
        return norm_lang(str(lang)), texts
    if ext in ('.html', '.htm'):
        p = _HTML()
        p.feed(raw)
        p.close()
        p.flush()
        return norm_lang(p.lang), p.out
    return '', parse_text(raw)


def looks_like_path(arg: str) -> bool:
    return not re.search(r'\s', arg) and arg.lower().endswith(FILE_EXTS)


# ---------------------------------------------------------------- layers

_DICT_CACHE: dict[str, Any] = {}


def load_dictionary(lang: str) -> tuple[Any, str]:
    """Return (spylls Dictionary or None, reason it could not load)."""
    name = HUNSPELL.get(lang)
    base = os.path.join(dicts_dir(), name or lang)
    if not name or not (os.path.exists(base + '.aff') and os.path.exists(base + '.dic')):
        return None, f'no {name or lang} dictionary in {dicts_dir()} (run: python fetch_dicts.py {lang})'
    if base not in _DICT_CACHE:
        try:
            from spylls.hunspell import Dictionary
        except ImportError:
            return None, 'spylls not installed (python -m pip install -r requirements.txt)'
        _DICT_CACHE[base] = Dictionary.from_files(base)
    return _DICT_CACHE[base], ''


def hunspell(texts: list[str], lang: str, ignored: Ignored) -> LayerResult:
    d, why = load_dictionary(lang)
    if d is None:
        return LayerResult(error=why)
    res = LayerResult()
    seen: set[str] = set()
    for t in texts:
        clean = re.sub(r'\S+@\S+|https?://\S+|\b[\w.-]+\.[a-z]{2,4}\b\S*', ' ', t)
        for w in re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", clean):
            if w in seen or len(w) < 2 or ignored(w) or (w.isupper() and len(w) <= 6):
                continue
            ok = d.lookup(w) or (w[0].isupper() and d.lookup(w.lower()))
            if not ok and re.search(r"['’]", w):
                ok = all(d.lookup(p) or ignored(p) for p in re.split(r"['’]", w) if len(p) > 1)
            if ok:
                continue
            seen.add(w)
            try:
                sug = list(d.suggest(w))[:3]
            except Exception:  # spylls can fail on odd affix data; the finding still stands
                sug = []
            res.findings.append(Finding('hunspell', w, sug, 'unknown word', t))
    return res


def _http_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f'HTTP {e.code}'
    if isinstance(e, urllib.error.URLError):
        return f'not reachable ({e.reason})'
    return e.__class__.__name__


def languagetool(texts: list[str], lang: str, ignored: Ignored) -> LayerResult:
    ltl = LT_LANG[lang]
    res = LayerResult()
    for t in texts:
        data = urllib.parse.urlencode({'text': t, 'language': ltl, 'level': 'picky',
                                       'disabledRules': LT_DISABLED}).encode()
        try:
            with urllib.request.urlopen(lt_url(), data=data, timeout=60) as r:
                matches = json.load(r)['matches']
        except Exception as e:
            res.error = f'{lt_url()}: {_http_error(e)}, grammar NOT checked'
            return res
        for m in matches:
            bad = t[m['offset']:m['offset'] + m['length']]
            if ignored(bad):
                continue
            res.findings.append(Finding('languagetool', bad, [x['value'] for x in m['replacements'][:3]],
                                        m['message'], t, rule=m['rule']['id']))
    return res


def korektor(texts: list[str], ignored: Ignored) -> LayerResult:
    res = LayerResult()
    for t in texts:
        data = urllib.parse.urlencode({'model': KOREKTOR_MODEL, 'data': t}).encode()
        try:
            with urllib.request.urlopen(KOREKTOR_URL, data=data, timeout=30) as r:
                fixed = json.load(r)['result']
        except Exception as e:
            res.error = f'{_http_error(e)}, Czech checked by dictionary only'
            return res
        a, b = re.findall(r'\S+', t), re.findall(r'\S+', fixed)
        if len(a) == len(b):
            pairs = list(zip(a, b, strict=True))
        else:
            pairs = [(t, fixed)] if fixed.strip() != t.strip() else []
        for x, y in pairs:
            if x != y and not ignored(x.strip('.,!?:;"()')):
                res.findings.append(Finding('korektor', x, [y], 'spelling/diacritics', t))
    return res


# ---------------------------------------------------------------- run

@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: dict[str, dict[str, Any]] = field(default_factory=dict)
    exit_code: int = EXIT_CLEAN


def _run_layer(name: str, texts: list[str], lang: str, ignored: Ignored) -> LayerResult:
    if name == 'hunspell':
        return hunspell(texts, lang, ignored)
    if name == 'languagetool':
        return languagetool(texts, lang, ignored)
    return korektor(texts, ignored)


def check(groups: dict[str, list[tuple[str, str]]], ignored: Ignored,
          online: bool = False, use_lt: bool = True, strict: bool = False) -> Report:
    """groups: lang -> [(source, text)]. Runs every layer that applies and sets the exit code."""
    rep = Report()
    incomplete = False
    for lang, items in groups.items():
        texts = list(dict.fromkeys(t for _, t in items if t.strip()))
        source: dict[str, str] = {}
        for s, t in items:
            source.setdefault(t, s)
        layers = ['hunspell']
        if lang in LT_LANG and use_lt:
            layers.append('languagetool')
        if lang == 'cs' and online:
            layers.append('korektor')
        ran: list[str] = []
        skipped: dict[str, str] = {}
        for name in layers:
            r = _run_layer(name, texts, lang, ignored) if texts else LayerResult()
            for f in r.findings:
                f.source = source.get(f.text, '')
            rep.findings += r.findings
            if r.error:
                skipped[name] = r.error
                rep.notes.append(f'[{name}] {r.error}')
            else:
                ran.append(name)
        rep.checked[lang] = {'texts': len(texts), 'ran': ran, 'skipped': skipped}
        if texts and (not ran or (strict and skipped)):
            incomplete = True
    if rep.findings:
        rep.exit_code = EXIT_FINDINGS
    elif incomplete:
        rep.exit_code = EXIT_UNCHECKED
    return rep


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='langcheck.py',
        description='Spelling and grammar gate for marketing copy (cs, sk, pl, de, en).',
        epilog='Exit codes: 0 clean, 1 findings, 2 bad input, 3 could not check. '
               'Environment: LANGTOOLS_DICTS (dictionary folder), LT_URL (LanguageTool server).')
    p.add_argument('inputs', nargs='+', metavar='FILE_OR_TEXT',
                   help='files (.txt .md .html .json), "-" for stdin, or the copy itself in quotes')
    p.add_argument('--lang', help='cs, sk, pl, de or en; required unless every file states its own')
    p.add_argument('--all-strings', action='store_true', help='JSON: check every string value, not only copy keys')
    p.add_argument('--ignore', action='append', default=[], metavar='FILE',
                   help='words that are right on purpose, one per line (repeatable)')
    p.add_argument('--online', action='store_true',
                   help='also use Korektor (public LINDAT API) for Czech; sends text to a third party, '
                        'non-commercial licence on its models')
    p.add_argument('--no-online', action='store_true', help='never send text out (the default; kept for scripts)')
    p.add_argument('--no-languagetool', action='store_true', help='skip the grammar layer')
    p.add_argument('--strict', action='store_true',
                   help='exit 3 when any applicable layer could not run (default: only when none ran)')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return p


def gather(inputs: list[str], lang_arg: str, all_strings: bool) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    loose: list[str] = []
    for arg in inputs:
        if arg == '-':
            items = parse_text(sys.stdin.read())
            flang, src = '', '<stdin>'
        elif os.path.isfile(arg):
            flang, items = collect(arg, all_strings)
            src = arg
        elif looks_like_path(arg):
            raise UsageError(f'file not found: {arg}')
        else:
            loose.append(arg)
            continue
        if not any(t.strip() for t in items):
            hint = ' (JSON: only copy keys are read, see --all-strings)' if src.lower().endswith('.json') else ''
            raise UsageError(f'{src}: no text found{hint}')
        lang = lang_arg or flang
        if not lang:
            raise UsageError(f'{src}: cannot tell the language; pass --lang cs|sk|pl|de|en')
        groups.setdefault(lang, []).extend((src, t) for t in items)
    if loose:
        if not lang_arg:
            raise UsageError('cannot tell the language of the text; pass --lang cs|sk|pl|de|en')
        groups.setdefault(lang_arg, []).extend(('<arg>', t) for t in loose)
    for lang in groups:
        if lang not in SUPPORTED:
            raise UsageError(f'language "{lang}" is not supported; use one of {", ".join(SUPPORTED)}')
    return groups


def render(rep: Report) -> str:
    lines = list(rep.notes)
    for f in rep.findings:
        sug = ', '.join(f.suggest) or '?'
        rule = f' {f.rule}' if f.rule else ''
        ctx = f.text if len(f.text) <= 120 else f.text[:117] + '...'
        lines.append(f'[{f.tool}{rule}] "{f.found}" -> {sug}  ({f.message})  in: {ctx}')
    for lang, c in rep.checked.items():
        layers = ', '.join(c['ran']) or 'nothing'
        lines.append(f"{c['texts']} texts checked in {lang} by {layers}")
    if rep.exit_code == EXIT_FINDINGS:
        lines.append(f'{len(rep.findings)} findings')
    elif rep.exit_code == EXIT_UNCHECKED:
        missing = [f"{lang} ({', '.join(c['skipped'])})" for lang, c in rep.checked.items() if c['skipped']]
        lines.append('NOT CHECKED, layers that did not run: ' + '; '.join(missing))
    else:
        lines.append('clean')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        lang_arg = norm_lang(args.lang) if args.lang else ''
        groups = gather(args.inputs, lang_arg, args.all_strings)
        default_ignore = os.path.join(HERE, 'ignore.txt')
        ignored = make_ignored(([default_ignore] if os.path.exists(default_ignore) else []) + args.ignore)
    except (UsageError, OSError, UnicodeDecodeError) as e:
        print(f'langcheck: {e}', file=sys.stderr)
        return EXIT_USAGE
    rep = check(groups, ignored, online=args.online and not args.no_online,
                use_lt=not args.no_languagetool, strict=args.strict)
    if args.json:
        print(json.dumps({'version': __version__, 'exit_code': rep.exit_code, 'checked': rep.checked,
                          'notes': rep.notes, 'findings': [asdict(f) for f in rep.findings]},
                         ensure_ascii=False, indent=1))
    else:
        print(render(rep))
    return rep.exit_code


if __name__ == '__main__':
    sys.exit(main())
