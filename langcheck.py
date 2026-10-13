"""Spelling and grammar check for marketing copy in Czech, Slovak, Polish, German and English.

Read-only: it never changes your text. Exit code 1 when anything is found, so it
can gate a build, a commit or an ad upload.

    python langcheck.py --lang cs "Pozvi kamaráda a získej odměnu"
    python langcheck.py --lang pl copy.txt              # plain text or Markdown, one check per paragraph
    python langcheck.py page.html ads.json              # several files; HTML/JSON can state their own language
    python langcheck.py --lang de-AT --all-strings app-strings.json   # every string value in the JSON
    echo "Text" | python langcheck.py --lang en-US -    # read from stdin

Languages are BCP 47 tags: cs, sk, pl, de (= de-DE), de-AT, de-CH, en (= en-GB), en-GB, en-US.
A language stated in the file (HTML lang, JSON "lang") wins over --lang; --force-lang overrides it.

Layers, each reported when it cannot run:
  1. Hunspell via spylls + LibreOffice dictionaries, fully local (python fetch_dicts.py).
  2. LanguageTool, a server you run (LT_URL, default http://localhost:8081/v2/check).
     Grammar, agreement, punctuation for sk, pl, de, en. It has no Czech module.
  3. Korektor (UFAL, Charles University) for Czech, via the public LINDAT API. Off
     unless you pass --online: it sends text out, and its models are CC BY-NC-SA
     (commercial use needs the authors' written permission).

Exit codes: 0 clean, 1 findings, 2 bad input, 3 could not check (a layer that applies
did not run on every text; with --lenient only when a text was checked by no layer).
"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import difflib
import html.parser
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

__version__ = '1.2.0'

HERE = os.path.dirname(os.path.abspath(__file__))
KOREKTOR_URL = 'https://lindat.mff.cuni.cz/services/korektor/api/correct'
KOREKTOR_MODEL = 'czech-spellchecker-130202'
KOREKTOR_BATCH_CHARS = 20000   # characters per Korektor request
# Texts in one Korektor request are separated by a blank line. With a single newline it reads a
# headline without a full stop and the next text as one sentence and lowercases the next word.
KOREKTOR_SEP = '\n\n'
RETRY_DELAY = 1.0              # seconds before the one retry of a failed LanguageTool/Korektor request

EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, EXIT_UNCHECKED = 0, 1, 2, 3

# Language tag -> (Hunspell dictionary from fetch_dicts.py, LanguageTool code or '' when it has none).
LANGS = {
    'cs-CZ': ('cs_CZ', ''),
    'sk-SK': ('sk_SK', 'sk-SK'),
    'pl-PL': ('pl_PL', 'pl-PL'),
    'de-DE': ('de_DE_frami', 'de-DE'),
    'de-AT': ('de_AT_frami', 'de-AT'),
    'de-CH': ('de_CH_frami', 'de-CH'),
    'en-GB': ('en_GB', 'en-GB'),
    'en-US': ('en_US', 'en-US'),
}
DEFAULT_REGION = {'cs': 'cs-CZ', 'sk': 'sk-SK', 'pl': 'pl-PL', 'de': 'de-DE', 'en': 'en-GB'}
SUPPORTED_TEXT = 'cs, sk, pl, de (de-DE), de-AT, de-CH, en (en-GB), en-US'
# Country codes people write where a language tag belongs. Only used to explain the error.
LANG_HINTS = {
    'cz': 'cz is the country code of Czechia, the language tag for Czech is cs',
    'uk': 'uk is Ukrainian, British English is en-GB',
    'gb': 'gb is a country code, British English is en-GB',
    'us': 'us is a country code, American English is en-US',
    'at': 'at is a country code, Austrian German is de-AT',
    'ch': 'ch is Chamorro, Swiss German is de-CH',
}
FILE_EXTS = ('.txt', '.md', '.markdown', '.html', '.htm', '.json')

# Platform, tech and currency words that are never typos in marketing copy. Case-sensitive like
# your own list: a lowercase entry also covers Capitalised and UPPERCASE, any entry covers UPPERCASE.
DEFAULT_IGNORE = (
    'iOS', 'Android', 'Google', 'Apple', 'Meta', 'TikTok', 'Instagram', 'Facebook', 'YouTube', 'WhatsApp',
    'LinkedIn', 'Visa', 'Mastercard', 'PayPal', 'API', 'CTA', 'FAQ', 'PDF', 'SMS', 'QR', 'PIN', 'IBAN',
    'GDPR', 'URL', 'EUR', 'USD', 'CZK', 'PLN', 'CHF', 'Kč', 'zł', 'app', 'apps', 'store', 'play', 'pay',
    'cashback', 'online', 'e-mail', 'email', 'newsletter', 'onboarding', 'fintech', 'premium', 'www',
)

# Picky-level LanguageTool rules that flag copy formatting (headlines without a full
# stop, typographic quotes, line fragments), not the language itself.
LT_DISABLED = (
    'WHITESPACE_RULE', 'PUNCTUATION_PARAGRAPH_END', 'UPPERCASE_SENTENCE_START', 'EN_QUOTES',
    'DASH_RULE', 'TYPOGRAFISCHE_ANFUEHRUNGSZEICHEN', 'APOS_TYP', 'TYPOGRAPHY', 'UNPAIRED_BRACKETS',
    'DOUBLE_PUNCTUATION', 'SENTENCE_WHITESPACE', 'PUNKT_ENDE_ABSATZ', 'EN_UNPAIRED_QUOTES',
    'PL_QUOTES', 'SK_QUOTES', 'ELLIPSIS', 'BRAK_KROPKI', 'TYPOGRAFISCHE_APOSTROPHE',
)
TEXT_KEYS = {'headline', 'headlines', 'title', 'subtitle', 'sub', 'subline', 'cta', 'text', 'copy',
             'body', 'primary_text', 'description', 'descriptions', 'long_headline', 'subject', 'preheader'}
META_KEYS = {'lang', 'language', 'market'}   # JSON keys that describe the copy, never checked
SKIP_TAGS = {'script', 'style', 'svg', 'math', 'template', 'noscript', 'code', 'pre', 'kbd', 'samp'}
# Only elements that end a run of text. Inline elements (a, span, b, em...) join their text
# with the words around them, and <br> becomes a space. Form controls and <title> are
# separate labels, so they count as blocks here.
BLOCK_TAGS = {
    'address', 'article', 'aside', 'blockquote', 'body', 'button', 'caption', 'dd', 'details', 'dialog',
    'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5',
    'h6', 'head', 'header', 'hgroup', 'hr', 'html', 'label', 'legend', 'li', 'main', 'nav', 'ol', 'optgroup',
    'option', 'p', 'section', 'select', 'summary', 'table', 'tbody', 'td', 'textarea', 'tfoot', 'th',
    'thead', 'title', 'tr', 'ul',
}
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source',
             'track', 'wbr'}
TEXT_ATTRS = ('alt', 'title', 'placeholder', 'aria-label')
META_TEXT = {'description', 'og:title', 'og:description', 'twitter:title', 'twitter:description'}

# Things that are not words: e-mail addresses, URLs, www hosts and bare domains with a known
# top-level domain. "vis.zkus" (a missing space) is not a domain, so both words get checked.
TLDS = ('com', 'org', 'net', 'info', 'biz', 'io', 'app', 'dev', 'ai', 'co', 'me', 'eu', 'cz', 'sk', 'pl',
        'de', 'at', 'ch', 'uk', 'us', 'fr', 'it', 'es', 'nl', 'be', 'hu', 'ro', 'ua', 'shop', 'store', 'online')
SKIP_RE = re.compile(
    r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'
    r'|\b[a-z][a-z0-9+.-]*://\S+'
    r'|\bwww\.\S+'
    r'|\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:' + '|'.join(TLDS) + r')\b(?![.-]?\w)(?:/\S*)?',
    re.I)
WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
TOKEN_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*|[^\w\s]")


class UsageError(Exception):
    """Bad input: unknown language, missing file, unreadable JSON. Exit code 2."""


class UnsupportedLanguage(UsageError):
    """A language tag that is malformed or has no dictionary here."""


@dataclass
class Finding:
    tool: str
    found: str
    suggest: list[str]
    message: str
    text: str
    rule: str = ''
    source: str = ''
    line: int = 0      # 1-based position in the source; 0 = unknown
    col: int = 0
    offset: int = 0    # character offset of `found` in `text`
    also: list[str] = field(default_factory=list)   # other layers that flagged the same span


@dataclass
class LayerResult:
    findings: list[Finding] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)   # texts this layer could not check
    error: str = ''


def dicts_dir() -> str:
    return os.environ.get('LANGTOOLS_DICTS') or os.path.join(HERE, 'dicts')


def lt_url() -> str:
    return os.environ.get('LT_URL') or 'http://localhost:8081/v2/check'


_TAG_RE = re.compile(r'^([a-z]{2,3})(?:-[a-z]{4})?(?:-([a-z]{2}|\d{3}))?(?:-[a-z0-9]{1,8})*$')


def norm_lang(raw: str) -> str:
    """BCP 47 tag -> supported tag (de -> de-DE, en_us -> en-US). Raises UnsupportedLanguage."""
    x = (raw or '').strip().replace('_', '-').lower()
    m = _TAG_RE.match(x)
    if m:
        lang, region = m.group(1), m.group(2)
        tag = f'{lang}-{region.upper()}' if region else DEFAULT_REGION.get(lang, lang)
        if tag in LANGS:
            return tag
    hint = f' ({LANG_HINTS[x]})' if x in LANG_HINTS else ''
    raise UnsupportedLanguage(f'language "{raw}" is not supported{hint}; use one of {SUPPORTED_TEXT}')


class IgnoreList:
    """Words that are right on purpose. Case-sensitive, Hunspell-style: "zorbly" also covers
    "Zorbly" and "ZORBLY", "Zorbly" also covers "ZORBLY" but not "zorbly". An entry written
    "i:word" matches in any case. A line "rule:RULE_ID" switches off a LanguageTool rule."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.exact: set[str] = set()
        self.lower: set[str] = set()
        self.upper: set[str] = set()
        self.any_case: set[str] = set()
        self.rules: list[str] = []
        for e in entries:
            self.add(e)

    def add(self, entry: str) -> None:
        e = entry.strip()
        if not e or e.startswith('#'):
            return
        if e.startswith('rule:'):
            self.rules.extend(r.strip() for r in e[5:].split(',') if r.strip())
        elif e.startswith('i:'):
            if e[2:].strip():
                self.any_case.add(e[2:].strip().lower())
        else:
            self.exact.add(e)
            self.upper.add(e.upper())
            if e == e.lower():
                self.lower.add(e)

    def __call__(self, w: str) -> bool:
        return (w in self.exact or w.lower() in self.any_case or (w.isupper() and w in self.upper)
                or (w[:1].isupper() and w[:1].lower() + w[1:] in self.lower))


def make_ignored(paths: Iterable[str], defaults: Iterable[str] = DEFAULT_IGNORE) -> IgnoreList:
    ign = IgnoreList(defaults)
    for p in paths:
        with open(p, encoding='utf-8') as f:
            for line in f:
                ign.add(line)
    return ign


Ignored = Callable[[str], bool]


# ---------------------------------------------------------------- input

def collapse(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


class _Lines:
    def __init__(self, raw: str) -> None:
        self.starts = [0] + [m.end() for m in re.finditer('\n', raw)]

    def linecol(self, i: int) -> tuple[int, int]:
        ln = bisect.bisect_right(self.starts, i)
        return ln, i - self.starts[ln - 1] + 1

    def index(self, line: int, col0: int) -> int:
        return self.starts[min(line, len(self.starts)) - 1] + col0


@dataclass
class Snippet:
    """One text to check, with where it came from."""
    text: str
    source: str = ''
    lang: str = ''       # language as stated in the source, '' = not stated
    scope: str = ''      # 'file' = stated for the whole file, 'element' = for a part of it
    line: int = 0        # start of `raw` in the source, 1-based; 0 = unknown
    col: int = 0
    raw: str = ''        # the stretch of the source the text was taken from

    def locate(self, offset: int, found: str) -> tuple[int, int]:
        """Line and column of `found` (at `offset` in text) in the source; falls back to the text start."""
        if not self.raw:
            return self.line, self.col
        pos = -1
        words = found.split()
        if words:
            k = len(re.findall(re.escape(found), self.text[:offset]))
            pattern = r'\s+'.join(map(re.escape, words))
            hits = [m.start() for m in re.finditer(pattern, self.raw)]
            if k < len(hits):
                pos = hits[k]
        if pos < 0:
            pos = len(self.raw) - len(self.raw.lstrip())
        prefix = self.raw[:pos]
        nl = prefix.count('\n')
        return self.line + nl, (self.col + pos) if nl == 0 else pos - prefix.rfind('\n')


def _snippet(text: str, source: str, lang: tuple[str, str], raw: str, lines: _Lines,
             start: int, end: int) -> Snippet:
    piece = raw[start:end]
    line, col = lines.linecol(start)
    return Snippet(collapse(text), source, lang[0], lang[1], line, col, piece)


def _blank(m: re.Match[str]) -> str:
    return re.sub(r'[^\n]', ' ', m.group())


def parse_text(raw: str, source: str = '') -> list[Snippet]:
    """Plain text or Markdown: one text per paragraph, markup blanked out (positions are kept)."""
    t = re.sub(r'```.*?```', _blank, raw, flags=re.S)                          # code blocks
    t = re.sub(r'`[^`\n]*`|!\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]+>', _blank, t)      # inline code, images, tags
    t = re.sub(r'\[([^\]\n]*)\]\([^)\n]*\)',                                   # links -> their text
               lambda m: ' ' + m.group(1) + ' ' * (len(m.group()) - len(m.group(1)) - 1), t)
    t = re.sub(r'^([ \t]{0,3})(#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+|>[ \t]?)',
               lambda m: m.group(1) + ' ' * len(m.group(2)), t, flags=re.M)
    t = t.replace('**', '  ').replace('__', '  ')
    lines = _Lines(raw)
    out = []
    for m in re.finditer(r'(?:^[^\S\n]*\S[^\n]*(?:\n|\Z))+', t, flags=re.M):
        text = collapse(m.group())
        if text:
            start = m.start() + len(m.group()) - len(m.group().lstrip())
            out.append(_snippet(text, source, ('', ''), raw, lines, start, m.end()))
    return out


class _HTML(html.parser.HTMLParser):
    def __init__(self, raw: str, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.raw, self.source, self.lines = raw, source, _Lines(raw)
        self.out: list[Snippet] = []
        self.buf: list[str] = []
        self.buf_start = 0
        self.buf_lang: tuple[str, str] = ('', '')
        self.stack: list[tuple[str, str, str]] = []   # (tag, lang, scope)

    def _here(self) -> int:
        line, col0 = self.getpos()
        return self.lines.index(line, col0)

    def _lang(self) -> tuple[str, str]:
        return (self.stack[-1][1], self.stack[-1][2]) if self.stack else ('', '')

    def _skipping(self) -> bool:
        return any(t in SKIP_TAGS for t, _, _ in self.stack)

    def flush(self, end: int | None = None) -> None:
        text = collapse(''.join(self.buf))
        if text:
            end = self._here() if end is None else end
            self.out.append(_snippet(text, self.source, self.buf_lang, self.raw, self.lines, self.buf_start, end))
        self.buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or '') for k, v in attrs}
        own = a.get('lang', a.get('xml:lang'))
        lang = self._lang() if own is None else (own.strip(), 'file' if tag == 'html' else 'element')
        if tag in BLOCK_TAGS:
            self.flush()
        if tag == 'br' and self.buf:
            self.buf.append(' ')
        if tag not in VOID_TAGS:
            self.stack.append((tag, *lang))
        if self._skipping():
            return
        texts = [a[k] for k in TEXT_ATTRS if a.get(k, '').strip()]
        if tag == 'meta' and (a.get('name', '').lower() in META_TEXT or a.get('property', '').lower() in META_TEXT):
            texts.append(a.get('content', ''))
        start = self._here()
        end = start + len(self.get_starttag_text() or '')
        for v in texts:
            if v.strip():
                self.out.append(_snippet(v, self.source, lang, self.raw, self.lines, start, end))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self.flush()
        if any(t == tag for t, _, _ in self.stack):
            while self.stack and self.stack.pop()[0] != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self._skipping():
            return
        lang = self._lang()
        if self.buf and lang != self.buf_lang and data.strip():
            self.flush()                      # a part in another language is its own text
        if not self.buf:
            if not data.strip():
                return
            self.buf_start = self._here() + len(data) - len(data.lstrip())
            self.buf_lang = lang
        self.buf.append(data)


def parse_html(raw: str, source: str = '') -> list[Snippet]:
    p = _HTML(raw, source)
    p.feed(raw)
    p.close()
    p.flush(end=len(raw))
    return p.out


def parse_json(raw: str, source: str = '', all_strings: bool = False) -> list[Snippet]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UsageError(f'{source}: not valid JSON ({e})') from None
    lines = _Lines(raw)
    out: list[Snippet] = []
    cursor = 0

    def walk(x: Any, key: str, lang: tuple[str, str], depth: int) -> None:
        nonlocal cursor
        if isinstance(x, dict):
            own = x.get('lang', x.get('language'))
            if isinstance(own, str) and own.strip():
                lang = (own.strip(), 'file' if depth == 0 else 'element')
            for k, v in x.items():
                if k not in META_KEYS:
                    walk(v, k, lang, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, key, lang, depth + 1)
        elif isinstance(x, str) and (all_strings or key in TEXT_KEYS) and x.strip():
            for lit in dict.fromkeys((json.dumps(x, ensure_ascii=False), json.dumps(x))):
                m = re.compile(r'[:\[,]\s*' + re.escape(lit)).search(raw, cursor)
                if m:
                    start = m.end() - len(lit)
                    cursor = m.end()
                    out.append(_snippet(x, source, lang, raw, lines, start + 1, m.end() - 1))
                    return
            out.append(Snippet(collapse(x), source, lang[0], lang[1]))
    walk(data, '', ('', ''), 0)
    return out


def collect(path: str, all_strings: bool = False) -> list[Snippet]:
    """Texts of a .json, .html or text/Markdown file, each with its stated language and position."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding='utf-8-sig') as f:
        raw = f.read()
    if ext == '.json':
        return parse_json(raw, path, all_strings)
    if ext in ('.html', '.htm'):
        return parse_html(raw, path)
    return parse_text(raw, path)


def looks_like_path(arg: str) -> bool:
    return not re.search(r'\s', arg) and arg.lower().endswith(FILE_EXTS)


# ---------------------------------------------------------------- layers

_DICT_CACHE: dict[str, Any] = {}


def load_dictionary(tag: str) -> tuple[Any, str]:
    """Return (spylls Dictionary or None, reason it could not load)."""
    name = LANGS[tag][0]
    base = os.path.join(dicts_dir(), name)
    if not (os.path.exists(base + '.aff') and os.path.exists(base + '.dic')):
        return None, f'no {name} dictionary in {dicts_dir()} (run: python fetch_dicts.py {tag})'
    if base not in _DICT_CACHE:
        try:
            from spylls.hunspell import Dictionary
        except ImportError:
            return None, 'spylls not installed (python -m pip install -r requirements.txt)'
        _DICT_CACHE[base] = Dictionary.from_files(base)
    return _DICT_CACHE[base], ''


def words(text: str) -> Iterator[tuple[str, int]]:
    """(word, offset) for every word worth a dictionary lookup."""
    clean = SKIP_RE.sub(lambda m: ' ' * len(m.group()), text)
    for m in WORD_RE.finditer(clean):
        w = m.group()
        if not any(c.isdigit() for c in w):
            if len(w) >= 2:
                yield w, m.start()
        elif len(w) > 4 and not w.isdigit():            # 2026, B2B, 4K and mp3 are skipped
            for p in re.finditer(r'[^\W\d_]{2,}', w):
                yield p.group(), m.start() + p.start()


def hunspell(texts: list[str], tag: str, ignored: Ignored) -> LayerResult:
    d, why = load_dictionary(tag)
    if d is None:
        return LayerResult(unchecked=list(texts), error=why)

    def known(w: str) -> bool:
        forms = [w]
        if w.isupper():
            forms += [w.lower(), w.capitalize()]     # STÁHNOUT is checked as stáhnout, PRAHA as Praha
        elif w[:1].isupper():
            forms.append(w.lower())
        return any(d.lookup(x) for x in forms)

    res = LayerResult()
    suggestions: dict[str, list[str]] = {}
    for t in texts:
        for w, off in words(t):
            if ignored(w) or known(w):
                continue
            if re.search(r"['’]", w) and all(known(p) or ignored(p) for p in re.split(r"['’]", w) if len(p) > 1):
                continue
            if w not in suggestions:
                try:
                    suggestions[w] = list(d.suggest(w))[:3]
                except Exception:  # spylls can fail on odd affix data; the finding still stands
                    suggestions[w] = []
            res.findings.append(Finding('hunspell', w, suggestions[w], 'unknown word', t, offset=off))
    return res


def _http_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f'HTTP {e.code}'
    if isinstance(e, urllib.error.URLError):
        return f'not reachable ({e.reason})'
    return e.__class__.__name__


def _server_down(e: Exception) -> bool:
    """True when the server is not there at all (as opposed to refusing one request)."""
    return not isinstance(e, urllib.error.HTTPError) and isinstance(e, (urllib.error.URLError, OSError))


def _post(url: str, form: dict[str, str], timeout: int = 60) -> Any:
    with urllib.request.urlopen(url, data=urllib.parse.urlencode(form).encode(), timeout=timeout) as r:
        return json.load(r)


def _with_retry(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        time.sleep(RETRY_DELAY)
        return fn()


def languagetool(texts: list[str], tag: str, ignored: Ignored, disabled: Iterable[str] = ()) -> LayerResult:
    code = LANGS[tag][1]
    off_rules = list(dict.fromkeys([*LT_DISABLED, *disabled]))
    off = set(off_rules)
    res = LayerResult()
    down = ''
    for t in texts:
        if down:
            res.unchecked.append(t)
            continue
        form = {'text': t, 'language': code, 'level': 'picky', 'disabledRules': ','.join(off_rules)}
        try:
            matches = _with_retry(lambda f=form: _post(lt_url(), f)['matches'])
        except Exception as e:
            res.unchecked.append(t)
            res.error = f'{lt_url()}: {_http_error(e)}, grammar NOT checked'
            if _server_down(e):
                down = res.error      # no point asking again for every remaining text
            continue
        for m in matches:
            if m['rule']['id'] in off:
                continue
            bad = t[m['offset']:m['offset'] + m['length']]
            if ignored(bad):
                continue
            res.findings.append(Finding('languagetool', bad, [x['value'] for x in m['replacements'][:3]],
                                        m['message'], t, rule=m['rule']['id'], offset=m['offset']))
    return res


def _fold(w: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', w.lower()) if not unicodedata.combining(c))


def _pair_tokens(old: list[str], new: list[str]) -> list[tuple[int, int]] | None:
    """Pair each old token with a similar new one, in order (kamarada ~ kamaráda). None if one has no partner."""
    if len(old) == len(new):
        return list(zip(range(len(old)), range(len(new)), strict=True))
    pairs, j = [], 0
    for i, w in enumerate(old):
        best, k_best = 0.0, -1
        for k in range(j, len(new)):
            r = difflib.SequenceMatcher(None, _fold(w), _fold(new[k])).ratio()
            if r > best:
                best, k_best = r, k
        if best < 0.6:
            return None
        pairs.append((i, k_best))
        j = k_best + 1
    return pairs


def korektor_diff(orig: str, fixed: str, ignored: Ignored) -> list[Finding]:
    """Align Korektor's corrected text with the original by tokens and character offsets."""
    a = list(TOKEN_RE.finditer(orig))
    b = [m.group() for m in TOKEN_RE.finditer(fixed)]
    out: list[Finding] = []

    def add(found: str, offset: int, suggestion: str) -> None:
        if found != suggestion and not ignored(found):
            out.append(Finding('korektor', found, [suggestion], 'spelling/diacritics', orig, offset=offset))

    sm = difflib.SequenceMatcher(None, [m.group() for m in a], b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            continue
        pairs = _pair_tokens([m.group() for m in a[i1:i2]], b[j1:j2]) if op == 'replace' else None
        if pairs is not None:
            for i, j in pairs:
                add(a[i1 + i].group(), a[i1 + i].start(), b[j1 + j])
        elif op in ('replace', 'delete'):
            add(orig[a[i1].start():a[i2 - 1].end()], a[i1].start(), ' '.join(b[j1:j2]))
        elif a:                                           # insert: anchor on the neighbouring word
            k = i1 - 1 if i1 > 0 else 0
            ins = ' '.join(b[j1:j2])
            add(a[k].group(), a[k].start(), f'{a[k].group()} {ins}' if i1 > 0 else f'{ins} {a[k].group()}')
    return out


def _batches(texts: list[str], limit: int) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for t in texts:
        if batch and size + len(t) > limit:
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t) + 1
    if batch:
        yield batch


def korektor(texts: list[str], ignored: Ignored) -> LayerResult:
    res = LayerResult()

    def ask(text: str) -> str:
        return str(_with_retry(lambda: _post(KOREKTOR_URL, {'model': KOREKTOR_MODEL, 'data': text})['result']))

    down = False
    for batch in _batches(texts, KOREKTOR_BATCH_CHARS):
        if down:
            res.unchecked += batch
            continue
        try:
            fixed = [x.strip() for x in ask(KOREKTOR_SEP.join(batch)).strip('\n').split(KOREKTOR_SEP)]
            if len(fixed) != len(batch):        # texts did not come back one to one: ask per text
                fixed = [ask(t).strip() for t in batch]
        except Exception as e:
            res.unchecked += batch
            res.error = f'{_http_error(e)}, Czech checked by dictionary only'
            down = _server_down(e)
            continue
        for t, f in zip(batch, fixed, strict=True):
            res.findings += korektor_diff(t, f, ignored)
    return res


# ---------------------------------------------------------------- run

@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: dict[str, dict[str, Any]] = field(default_factory=dict)
    unsupported: dict[str, int] = field(default_factory=dict)
    strict: bool = True
    exit_code: int = EXIT_CLEAN


def _run_layer(name: str, texts: list[str], tag: str, ignored: Ignored, disabled: Iterable[str]) -> LayerResult:
    if name == 'hunspell':
        return hunspell(texts, tag, ignored)
    if name == 'languagetool':
        return languagetool(texts, tag, ignored, disabled)
    return korektor(texts, ignored)


def merge_same_span(findings: list[Finding]) -> list[Finding]:
    """One finding per span: a later layer flagging the same word adds itself to `also`."""
    first: dict[tuple[str, int, str], Finding] = {}
    out = []
    for f in findings:
        key = (f.text, f.offset, f.found)
        if key in first:
            g = first[key]
            g.also.append(f'{f.tool} {f.rule}'.strip())
            g.suggest = list(dict.fromkeys(g.suggest + f.suggest))[:5]
        else:
            first[key] = f
            out.append(f)
    return out


def check(groups: dict[str, list[Snippet]], ignored: Ignored, *, online: bool = False, use_lt: bool = True,
          strict: bool = True, disabled_rules: Iterable[str] = (),
          unsupported: dict[str, list[Snippet]] | None = None) -> Report:
    """groups: language tag -> snippets. Runs every layer that applies and sets the exit code."""
    rep = Report(strict=strict)
    incomplete = False
    order = {}
    for tag, snippets in groups.items():
        for s in snippets:
            order.setdefault(s.source, len(order))
        texts = list(dict.fromkeys(s.text for s in snippets))
        layers = ['hunspell']
        if use_lt and LANGS[tag][1]:
            layers.append('languagetool')
        if online and tag == 'cs-CZ':
            layers.append('korektor')
        covered = dict.fromkeys(texts, 0)
        ran: list[str] = []
        partly: list[str] = []
        skipped: dict[str, str] = {}
        found: list[Finding] = []
        for name in layers:
            r = _run_layer(name, texts, tag, ignored, disabled_rules)
            missed = set(r.unchecked)
            for t in texts:
                covered[t] += t not in missed
            if 0 < len(missed) < len(texts):
                partly.append(name)
            if missed:
                why = r.error or 'not checked'
                if len(missed) < len(texts):
                    why = f'{len(missed)} of {len(texts)} texts not checked: {why}'
                skipped[name] = why
                rep.notes.append(f'[{name}] {tag}: {skipped[name]}')
            else:
                ran.append(name)
            found += r.findings
        by_text: dict[str, list[Snippet]] = {}
        for s in snippets:
            by_text.setdefault(s.text, []).append(s)
        for f in merge_same_span(found):
            for s in by_text[f.text]:
                line, col = s.locate(f.offset, f.found)
                rep.findings.append(dataclasses.replace(f, source=s.source, line=line, col=col,
                                                        suggest=list(f.suggest), also=list(f.also)))
        never = sum(1 for v in covered.values() if v == 0)
        rep.checked[tag] = {'texts': len(texts), 'layers': layers, 'ran': ran, 'partly': partly, 'skipped': skipped,
                            'unchecked_texts': never}
        if never or (strict and skipped):
            incomplete = True
    for lang, snippets in (unsupported or {}).items():
        rep.unsupported[lang] = len(snippets)
        s = snippets[0]
        rep.notes.append(f'{len(snippets)} texts marked lang="{lang}" were not checked, language not supported '
                         f'(first at {_where(s.source, s.line, s.col)})')
        incomplete = incomplete or strict
    rep.findings.sort(key=lambda f: (order.get(f.source, 0), f.line, f.col))
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
    p.add_argument('--lang', help=f'language for texts that do not state their own: {SUPPORTED_TEXT}')
    p.add_argument('--force-lang', metavar='LANG',
                   help='use this language for everything, even where a file states another')
    p.add_argument('--all-strings', action='store_true', help='JSON: check every string value, not only copy keys')
    p.add_argument('--ignore', action='append', default=[], metavar='FILE',
                   help='words that are right on purpose, one per line (repeatable); i:word = any case, '
                        'rule:ID = switch off a LanguageTool rule')
    p.add_argument('--disable-rule', action='append', default=[], metavar='ID',
                   help='switch off a LanguageTool rule (repeatable, or comma-separated)')
    p.add_argument('--online', action='store_true',
                   help='also use Korektor (public LINDAT API) for Czech; sends text to a third party, '
                        'non-commercial licence on its models')
    p.add_argument('--no-online', action='store_true', help='deprecated: off is the default; overrides --online')
    p.add_argument('--no-languagetool', action='store_true', help='skip the grammar layer')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--strict', action='store_true',
                      help='exit 3 when any applicable layer did not check every text (the default)')
    mode.add_argument('--lenient', action='store_true',
                      help='exit 3 only when a text was checked by no layer at all')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--json-out', metavar='FILE', help='also write the machine-readable report to FILE')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return p


def _where(source: str, line: int, col: int) -> str:
    return f'{source}:{line}:{col}' if line else source


def gather(inputs: list[str], lang_arg: str = '', force_lang: str = '',
           all_strings: bool = False) -> tuple[dict[str, list[Snippet]], dict[str, list[Snippet]]]:
    """Return (language tag -> snippets, unsupported element-level language -> snippets)."""
    groups: dict[str, list[Snippet]] = {}
    unsupported: dict[str, list[Snippet]] = {}
    n_args = 0
    for arg in inputs:
        if arg == '-':
            src = '<stdin>'
            snippets = parse_text(sys.stdin.read(), src)
        elif os.path.isfile(arg):
            src = arg
            snippets = collect(arg, all_strings)
        elif looks_like_path(arg):
            raise UsageError(f'file not found: {arg}')
        else:
            n_args += 1
            src = f'<arg{n_args}>'
            snippets = [Snippet(collapse(arg), src, line=1, col=1, raw=arg)] if arg.strip() else []
        if not snippets:
            hint = ' (JSON: only copy keys are read, see --all-strings)' if src.lower().endswith('.json') else ''
            raise UsageError(f'{src}: no text found{hint}')
        for s in snippets:
            if force_lang:
                tag = force_lang
            elif s.lang:
                try:
                    tag = norm_lang(s.lang)
                except UnsupportedLanguage as e:
                    if s.scope == 'element':
                        unsupported.setdefault(s.lang, []).append(s)
                        continue
                    raise UsageError(f'{_where(s.source, s.line, s.col)}: {e} (or pass --force-lang)') from None
            elif lang_arg:
                tag = lang_arg
            else:
                raise UsageError(f'{src}: cannot tell the language; pass --lang (one of {SUPPORTED_TEXT})')
            groups.setdefault(tag, []).append(s)
    return groups, unsupported


def render(rep: Report) -> str:
    lines = list(rep.notes)
    for f in rep.findings:
        sug = ', '.join(f.suggest) or '?'
        tools = ' '.join([f.tool + (f' {f.rule}' if f.rule else ''), *(f'+{a}' for a in f.also)])
        ctx = f.text if len(f.text) <= 120 else f.text[:117] + '...'
        lines.append(f'{_where(f.source, f.line, f.col)}: [{tools}] "{f.found}" -> {sug}  ({f.message})  in: {ctx}')
    for tag, c in rep.checked.items():
        layers = ', '.join(c['ran'] + [f'{x} (partly)' for x in c['partly']]) or 'nothing'
        lines.append(f"{c['texts']} texts checked in {tag} by {layers}")
    if rep.exit_code == EXIT_FINDINGS:
        lines.append(f'{len(rep.findings)} findings')
    elif rep.exit_code == EXIT_UNCHECKED:
        missing = [f"{tag} ({', '.join(c['skipped'])})" for tag, c in rep.checked.items() if c['skipped']]
        missing += [f'lang="{lang}" (not supported)' for lang in rep.unsupported]
        hint = '' if not rep.strict else '; --lenient accepts this when every text was checked by at least one layer'
        lines.append('NOT CHECKED, did not run: ' + '; '.join(missing) + hint)
    else:
        lines.append('clean')
    return '\n'.join(lines)


def report_json(rep: Report) -> str:
    return json.dumps({'version': __version__, 'exit_code': rep.exit_code, 'strict': rep.strict,
                       'checked': rep.checked, 'unsupported': rep.unsupported, 'notes': rep.notes,
                       'findings': [asdict(f) for f in rep.findings]}, ensure_ascii=False, indent=1)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if args.no_online:
        print('langcheck: --no-online is deprecated (Korektor is off unless --online) and will be removed in 2.0',
              file=sys.stderr)
    try:
        lang_arg = norm_lang(args.lang) if args.lang else ''
        force = norm_lang(args.force_lang) if args.force_lang else ''
        groups, unsupported = gather(args.inputs, lang_arg, force, args.all_strings)
        default_ignore = os.path.join(HERE, 'ignore.txt')
        ignored = make_ignored(([default_ignore] if os.path.exists(default_ignore) else []) + args.ignore)
    except (UsageError, OSError, UnicodeDecodeError) as e:
        print(f'langcheck: {e}', file=sys.stderr)
        return EXIT_USAGE
    disabled = ignored.rules + [r.strip() for x in args.disable_rule for r in x.split(',') if r.strip()]
    rep = check(groups, ignored, online=args.online and not args.no_online, use_lt=not args.no_languagetool,
                strict=not args.lenient, disabled_rules=disabled, unsupported=unsupported)
    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as f:
            f.write(report_json(rep) + '\n')
    print(report_json(rep) if args.json else render(rep))
    return rep.exit_code


if __name__ == '__main__':
    sys.exit(main())
