"""Spelling and grammar check for marketing copy in Czech, Slovak, Polish, German and English.

Read-only: it never changes your text. Exit code 1 when anything is found, so it
can gate a build, a commit or an ad upload.

    python langcheck.py --lang cs --text "Pozvi kamaráda a získej odměnu"
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
3 wins over 1: findings are still listed, but fixing them would not make the copy clean.
"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import difflib
import html
import html.parser
import http.client
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

__version__ = '1.4.0'

HERE = os.path.dirname(os.path.abspath(__file__))
KOREKTOR_URL = 'https://lindat.mff.cuni.cz/services/korektor/api/correct'
KOREKTOR_MODEL = 'czech-spellchecker-130202'
KOREKTOR_BATCH_CHARS = 20000   # characters per Korektor request
# Texts in one Korektor request are separated by a blank line. With a single newline it reads a
# headline without a full stop and the next text as one sentence and lowercases the next word.
KOREKTOR_SEP = '\n\n'
RETRY_DELAY = 1.0              # seconds before the one retry of a failed LanguageTool/Korektor request
KOREKTOR_FALLBACK_MAX = 20     # per-text Korektor requests per run when a batch does not come back one to one
LT_WORKERS = 4                 # LanguageTool requests in flight at once
SUGGEST_LIMIT = 20             # unknown words per language that get Hunspell suggestions (about 1 to 4 s each)

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
# Skipped elements that leave nothing behind. Other skipped inline elements (code, kbd, samp,
# translate="no") become PLACEHOLDER, so the sentence around them still reads as a sentence.
NO_PLACEHOLDER = {'script', 'style', 'svg', 'math', 'template', 'noscript'}
PLACEHOLDER = 'X'   # read as a noun by LanguageTool, too short for a dictionary lookup
FILL = '\x00'        # pads a placeholder to the length of what it replaced; dropped from the text
# Only elements that end a run of text. Inline elements (a, span, b, em...) join their text
# with the words around them, and <br> becomes a space. Form controls and <title> are
# separate labels, so they count as blocks here.
BLOCK_TAGS = {
    'address', 'article', 'aside', 'blockquote', 'body', 'button', 'caption', 'dd', 'details', 'dialog',
    'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5',
    'h6', 'head', 'header', 'hgroup', 'hr', 'html', 'label', 'legend', 'li', 'main', 'nav', 'ol', 'optgroup',
    'option', 'p', 'pre', 'section', 'select', 'summary', 'table', 'tbody', 'td', 'textarea', 'tfoot', 'th',
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
    note: str = ''                                      # worth saying, but the check itself is complete


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


def _collapse_mapped(s: str, pos: list[int]) -> tuple[str, list[int]]:
    """collapse() that keeps the source offset of every character it keeps."""
    chars: list[str] = []
    out: list[int] = []
    gap = -1
    for c, p in zip(s, pos, strict=True):
        if c.isspace():
            if chars and gap < 0:
                gap = p
            continue
        if gap >= 0:
            chars.append(' ')
            out.append(gap)
            gap = -1
        chars.append(c)
        out.append(p)
    return ''.join(chars), out


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
    line: int = 0        # start of the text in the source, 1-based; 0 = unknown
    col: int = 0
    pos: list[int] = field(default_factory=list, repr=False)   # source offset of each character of `text`
    lines: _Lines | None = field(default=None, repr=False, compare=False)

    def locate(self, offset: int, found: str = '') -> tuple[int, int]:
        """Line and column in the source of the character at `offset` in the text."""
        if not self.pos or self.lines is None:
            return self.line, self.col
        return self.lines.linecol(self.pos[min(max(offset, 0), len(self.pos) - 1)])


def _mapped(text: str, pos: list[int], source: str, lang: tuple[str, str], lines: _Lines,
            placeholders: Iterable[int] = ()) -> Snippet | None:
    """A snippet from extracted text and its source offsets; None when nothing but skipped markup is left."""
    if FILL in text:
        kept = [(c, p) for c, p in zip(text, pos, strict=True) if c != FILL]
        text, pos = ''.join(c for c, _ in kept), [p for _, p in kept]
    text, pos = _collapse_mapped(text, pos)
    if not text:
        return None
    ph = set(placeholders)
    if ph and not any(c.isalnum() for c, p in zip(text, pos, strict=True) if p not in ph):
        return None
    line, col = lines.linecol(pos[0])
    return Snippet(text, source, lang[0], lang[1], line, col, pos, lines)


def _plain(raw: str, source: str) -> Snippet | None:
    return _mapped(raw, list(range(len(raw))), source, ('', ''), _Lines(raw))


def _blank(m: re.Match[str]) -> str:
    return re.sub(r'[^\n]', ' ', m.group())


_FRONT_RE = re.compile(r'\A---[ \t]*\r?\n(.*?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)', re.S)
_FRONT_KEY_RE = re.compile(r'^(title|description)[ \t]*:[ \t]*(.*?)[ \t]*$', re.M)


def _front_matter(raw: str) -> tuple[int, list[tuple[str, list[int]]]]:
    """YAML front matter at the very start: (its length, [(title or description value, offsets)])."""
    m = _FRONT_RE.match(raw)
    if not m:
        return 0, []
    body_start, body = m.start(1), m.group(1) or ''
    values = []
    for k in _FRONT_KEY_RE.finditer(body):
        v, start = k.group(2), body_start + k.start(2)
        if v[:1] in ('>', '|'):                       # block scalar: the indented lines below it
            chars: list[str] = []
            pos: list[int] = []
            i = body_start + k.end()
            for ln in re.finditer(r'\n([ \t]+)(\S[^\n]*)', body[k.end():]):
                if ln.start() and body[k.end():][ln.start() - 1] != '\n' and not chars:
                    break
                a, b = i + ln.start(2), i + ln.end(2)
                chars.extend(' ' + raw[a:b])
                pos.extend([a, *range(a, b)])
            if chars:
                values.append((''.join(chars), pos))
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v, start = v[1:-1], start + 1
        if v.strip():
            values.append((v, list(range(start, start + len(v)))))
    return m.end(), values


def parse_text(raw: str, source: str = '') -> list[Snippet]:
    """Plain text or Markdown: one text per paragraph. Markup is blanked out and inline code becomes
    a placeholder, both keeping every position, so a finding points at its place in the source.
    YAML front matter is skipped except its title and description."""
    lines = _Lines(raw)
    out: list[Snippet] = []
    front, values = _front_matter(raw)
    for v, pos in values:
        s = _mapped(v, pos, source, ('', ''), lines)
        if s:
            out.append(s)
    placeholders: list[int] = []

    def code(m: re.Match[str]) -> str:
        placeholders.append(m.start())
        return PLACEHOLDER + FILL * (len(m.group()) - len(PLACEHOLDER))   # same length, no extra space

    t = ' ' * front + raw[front:]
    t = re.sub(r'```.*?```', _blank, t, flags=re.S)                             # code blocks
    t = re.sub(r'<!--.*?-->', _blank, t, flags=re.S)                            # comments, also multi-line
    t = re.sub(r'`[^`\n]+`', code, t)                                           # inline code -> placeholder
    t = re.sub(r'!\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]+>', _blank, t)                # images, tags
    t = re.sub(r'\[([^\]\n]*)\]\([^)\n]*\)',                                   # links -> their text
               lambda m: ' ' + m.group(1) + ' ' * (len(m.group()) - len(m.group(1)) - 1), t)
    t = re.sub(r'^([ \t]{0,3})(#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+|>[ \t]?)',
               lambda m: m.group(1) + ' ' * len(m.group(2)), t, flags=re.M)
    t = t.replace('**', '  ').replace('__', '  ')
    for m in re.finditer(r'(?:^[^\S\n]*\S[^\n]*(?:\n|\Z))+', t, flags=re.M):
        s = _mapped(m.group(), list(range(m.start(), m.end())), source, ('', ''), lines, placeholders)
        if s:
            out.append(s)
    return out


_REF_RE = re.compile(r'&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]*;?)')


def _decode(raw: str, start: int, end: int, want: str) -> list[int]:
    """Offsets in raw[start:end] of each character of `want`, the same stretch with character
    references decoded. When the two do not line up, offsets count from `start`."""
    chars: list[str] = []
    pos: list[int] = []
    i = start
    while i < end and len(chars) < len(want):
        m = _REF_RE.match(raw, i, end) if raw[i] == '&' else None
        d = html.unescape(m.group()) if m else ''
        if m and d != m.group():
            chars.extend(d)
            pos.extend([i] * len(d))
            i = m.end()
        else:
            chars.append(raw[i])
            pos.append(i)
            i += 1
    if ''.join(chars)[:len(want)] != want:
        return [min(start + k, max(end - 1, start)) for k in range(len(want))]
    return pos[:len(want)]


_ATTR_RE = re.compile(r'''([^\s/>"'=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''')


def _attr_spans(tag_text: str) -> dict[str, tuple[int, int]]:
    """Where each attribute value sits inside the raw start tag (the last one wins, as in HTMLParser)."""
    spans = {}
    name = re.match(r'<[^\s/>]+', tag_text)
    for m in _ATTR_RE.finditer(tag_text, name.end() if name else 0):
        for g in (2, 3, 4):
            if m.group(g) is not None:
                spans[m.group(1).lower()] = m.span(g)
                break
        else:
            spans[m.group(1).lower()] = (m.end(), m.end())
    return spans


class _HTML(html.parser.HTMLParser):
    def __init__(self, raw: str, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.raw, self.source, self.lines = raw, source, _Lines(raw)
        self.out: list[Snippet] = []
        self.chars: list[str] = []
        self.pos: list[int] = []
        self.ph: list[int] = []
        self.buf_lang: tuple[str, str] = ('', '')
        self.stack: list[tuple[str, str, str, bool, bool]] = []   # (tag, lang, scope, skipped, inside SKIP_TAGS)

    def _here(self) -> int:
        line, col0 = self.getpos()
        return self.lines.index(line, col0)

    def _lang(self) -> tuple[str, str]:
        return (self.stack[-1][1], self.stack[-1][2]) if self.stack else ('', '')

    def _skipping(self) -> bool:
        return bool(self.stack) and self.stack[-1][3]

    def _add(self, text: str, pos: list[int], lang: tuple[str, str]) -> None:
        if self.chars and lang != self.buf_lang and text.strip():
            self.flush()                      # a part in another language is its own text
        if not self.chars:
            if not text.strip():
                return
            self.buf_lang = lang
        self.chars.extend(text)
        self.pos.extend(pos)

    def flush(self) -> None:
        s = _mapped(''.join(self.chars), self.pos, self.source, self.buf_lang, self.lines, self.ph)
        if s:
            self.out.append(s)
        self.chars, self.pos, self.ph = [], [], []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or '') for k, v in attrs}
        own = a.get('lang', a.get('xml:lang'))
        parent_lang = self._lang()
        lang = parent_lang if own is None else (own.strip(), 'file' if tag == 'html' else 'element')
        parent_skip = self._skipping()
        hard = tag in SKIP_TAGS or (bool(self.stack) and self.stack[-1][4])
        translate = (a.get('translate') or '').strip().lower() if 'translate' in a else None
        if hard:
            skip = True
        elif translate in ('no', 'yes', ''):
            skip = translate == 'no'                  # translate="yes" inside "no" is copy again
        else:
            skip = parent_skip or 'notranslate' in a.get('class', '').split()
        if tag in BLOCK_TAGS:
            self.flush()
        start = self._here()
        if tag == 'br' and self.chars and not parent_skip:
            self._add(' ', [start], self.buf_lang)
        if skip and not parent_skip and tag not in VOID_TAGS | BLOCK_TAGS | NO_PLACEHOLDER:
            self._add(PLACEHOLDER, [start] * len(PLACEHOLDER), parent_lang)   # inline code, a name: keep the sentence
            self.ph.append(start)
        if tag not in VOID_TAGS:
            self.stack.append((tag, *lang, skip, hard))
        if skip:
            return
        names = [k for k in TEXT_ATTRS if a.get(k, '').strip()]
        if tag == 'meta' and (a.get('name', '').lower() in META_TEXT or a.get('property', '').lower() in META_TEXT):
            names.append('content')
        if not names:
            return
        spans = _attr_spans(self.get_starttag_text() or '')
        for k in names:
            v = a.get(k, '')
            if not v.strip():
                continue
            b, e = spans.get(k, (0, 0))
            s = _mapped(v, _decode(self.raw, start + b, start + e, v), self.source, lang, self.lines)
            if s:
                self.out.append(s)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self.flush()
        if any(t[0] == tag for t in self.stack):
            while self.stack and self.stack.pop()[0] != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self._skipping():
            return
        start = self._here()
        self._add(data, _decode(self.raw, start, len(self.raw), data), self._lang())


def parse_html(raw: str, source: str = '') -> list[Snippet]:
    p = _HTML(raw, source)
    p.feed(raw)
    p.close()
    p.flush()
    return p.out


_JSON_ESC = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}
_JSON_SCALAR = re.compile(r'[^\s,\]}]+')


def json_string_offsets(raw: str) -> dict[tuple[Any, ...], tuple[str, list[int]]]:
    """Every string value in a valid JSON document: path (keys and list indices) -> (value, the
    source offset of each of its characters). A repeated key keeps the last value, as json.loads does."""
    out: dict[tuple[Any, ...], tuple[str, list[int]]] = {}
    n = len(raw)

    def ws(i: int) -> int:
        while i < n and raw[i] in ' \t\r\n':
            i += 1
        return i

    def string(i: int) -> tuple[str, list[int], int]:
        chars: list[str] = []
        pos: list[int] = []
        i += 1
        while raw[i] != '"':
            if raw[i] != '\\':
                chars.append(raw[i])
                pos.append(i)
                i += 1
                continue
            e = raw[i + 1]
            if e != 'u':
                chars.append(_JSON_ESC.get(e, e))
                pos.append(i)
                i += 2
                continue
            code, j = int(raw[i + 2:i + 6], 16), i + 6
            if 0xD800 <= code < 0xDC00 and raw[j:j + 2] == '\\u':
                low = int(raw[j + 2:j + 6], 16)
                if 0xDC00 <= low < 0xE000:
                    code, j = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00), j + 6
            chars.append(chr(code))
            pos.append(i)
            i = j
        return ''.join(chars), pos, i + 1

    def value(i: int, path: tuple[Any, ...]) -> int:
        i = ws(i)
        c = raw[i]
        if c in '{[':
            close, k = ('}' if c == '{' else ']'), 0
            i = ws(i + 1)
            if raw[i] == close:
                return i + 1
            while True:
                if c == '{':
                    key, _, i = string(ws(i))
                    i = value(ws(i) + 1, (*path, key))         # + 1 skips the colon
                else:
                    i = value(i, (*path, k))
                    k += 1
                i = ws(i)
                if raw[i] == ',':
                    i = ws(i + 1)
                    continue
                return i + 1
        if c == '"':
            v, pos, i = string(i)
            out[path] = (v, pos)
            return i
        m = _JSON_SCALAR.match(raw, i)
        return m.end() if m else i + 1

    value(0, ())
    return out


def parse_json(raw: str, source: str = '', all_strings: bool = False) -> list[Snippet]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UsageError(f'{source}: not valid JSON ({e})') from None
    lines = _Lines(raw)
    offsets = json_string_offsets(raw)
    out: list[Snippet] = []

    def walk(x: Any, key: str, path: tuple[Any, ...], lang: tuple[str, str], depth: int) -> None:
        if isinstance(x, dict):
            own = x.get('lang', x.get('language'))
            if isinstance(own, str) and own.strip():
                lang = (own.strip(), 'file' if depth == 0 else 'element')
            for k, v in x.items():
                if k not in META_KEYS:
                    walk(v, k, (*path, k), lang, depth + 1)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, key, (*path, i), lang, depth + 1)
        elif isinstance(x, str) and (all_strings or key in TEXT_KEYS) and x.strip():
            v, pos = offsets.get(path, ('', []))
            s = _mapped(x, pos, source, lang, lines) if v == x else None
            out.append(s or Snippet(collapse(x), source, lang[0], lang[1]))
    walk(data, '', (), ('', ''), 0)
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
    """No spaces, and a path separator or something like a file extension, known or not."""
    if not arg or re.search(r'\s', arg):
        return False
    return '/' in arg or '\\' in arg or bool(re.search(r'\.(?=[0-9]*[^\W\d_])\w{1,10}$', arg))


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


_SUGGEST_CACHE: dict[tuple[str, str, str], list[str]] = {}


def hunspell(texts: list[str], tag: str, ignored: Ignored, suggest_limit: int = SUGGEST_LIMIT) -> LayerResult:
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

    def suggest(w: str) -> list[str]:
        key = (dicts_dir(), tag, w)
        if key in _SUGGEST_CACHE:
            return _SUGGEST_CACHE[key]
        if len(asked) >= suggest_limit:
            without.add(w)
            return []
        asked.add(w)
        try:
            _SUGGEST_CACHE[key] = list(d.suggest(w))[:3]
        except Exception:  # spylls can fail on odd affix data; the finding still stands
            _SUGGEST_CACHE[key] = []
        return _SUGGEST_CACHE[key]

    res = LayerResult()
    asked: set[str] = set()
    without: set[str] = set()
    for t in texts:
        for w, off in words(t):
            if ignored(w) or known(w):
                continue
            if re.search(r"['’]", w) and all(known(p) or ignored(p) for p in re.split(r"['’]", w)
                                                  if len(p) > 1):
                continue
            res.findings.append(Finding('hunspell', w, suggest(w), 'unknown word', t, offset=off))
    if without and suggest_limit > 0:
        res.note = (f'suggestions for the first {suggest_limit} unknown words only, {len(without)} more listed '
                    f'without (each takes seconds; --suggest-limit N to change, --no-suggest for none)')
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


def _retryable(e: Exception) -> bool:
    """Worth one more try: the network, a timeout or a server error. Not a 4xx or a bad answer."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500
    return isinstance(e, (OSError, http.client.HTTPException))


def _with_retry(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:
        if not _retryable(e):
            raise
        time.sleep(RETRY_DELAY)
        return fn()


def languagetool(texts: list[str], tag: str, ignored: Ignored, disabled: Iterable[str] = ()) -> LayerResult:
    """Up to LT_WORKERS requests at once. Every failure is counted; when the server is gone,
    texts not sent yet are not sent at all."""
    code = LANGS[tag][1]
    off_rules = list(dict.fromkeys([*LT_DISABLED, *disabled]))
    off = set(off_rules)
    down = threading.Event()
    down_why: list[str] = []

    def one(t: str) -> tuple[list[dict[str, Any]] | None, str]:
        if down.is_set():
            return None, ''
        form = {'text': t, 'language': code, 'level': 'picky', 'disabledRules': ','.join(off_rules)}
        try:
            return _with_retry(lambda: _post(lt_url(), form)['matches']), ''
        except Exception as e:
            why = _http_error(e)
            if _server_down(e):
                down_why.append(why)
                down.set()            # no point asking again for every remaining text
            return None, why

    res = LayerResult()
    if not texts:
        return res
    with ThreadPoolExecutor(max_workers=min(LT_WORKERS, len(texts))) as pool:
        results = list(pool.map(one, texts))
    errors: dict[str, int] = {}
    for t, (matches, why) in zip(texts, results, strict=True):
        if matches is None:
            res.unchecked.append(t)
            why = why or down_why[0]
            errors[why] = errors.get(why, 0) + 1
            continue
        for m in matches:
            if m['rule']['id'] in off:
                continue
            bad = t[m['offset']:m['offset'] + m['length']]
            if ignored(bad):
                continue
            res.findings.append(Finding('languagetool', bad, [x['value'] for x in m['replacements'][:3]],
                                        m['message'], t, rule=m['rule']['id'], offset=m['offset']))
    if errors:
        agg = '; '.join(f'{why} ({n} text{"s" if n > 1 else ""})' for why, n in errors.items())
        res.error = f'{lt_url()}: {agg}, grammar NOT checked'
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
    errors: list[str] = []

    def ask(text: str) -> str:
        return str(_with_retry(lambda: _post(KOREKTOR_URL, {'model': KOREKTOR_MODEL, 'data': text})['result']))

    down = False
    budget = KOREKTOR_FALLBACK_MAX
    for batch in _batches(texts, KOREKTOR_BATCH_CHARS):
        if down:
            res.unchecked += batch
            continue
        try:
            fixed: list[str | None] = [x.strip() for x in ask(KOREKTOR_SEP.join(batch)).strip('\n').split(KOREKTOR_SEP)]
            if len(fixed) != len(batch):        # texts did not come back one to one: ask per text, up to a cap
                fixed = []
                for t in batch:
                    if budget <= 0:
                        fixed.append(None)
                        continue
                    budget -= 1
                    fixed.append(ask(t).strip())
        except Exception as e:
            res.unchecked += batch
            errors.append(f'{_http_error(e)}, Czech checked by dictionary only')
            down = _server_down(e)
            continue
        for t, f in zip(batch, fixed, strict=True):
            if f is None:
                res.unchecked.append(t)
            else:
                res.findings += korektor_diff(t, f, ignored)
        if None in fixed:
            errors.append(f'batches did not come back one to one and the per-text fallback is capped at '
                          f'{KOREKTOR_FALLBACK_MAX} requests, the rest checked by dictionary only')
    res.error = '; '.join(dict.fromkeys(errors))
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


def _run_layer(name: str, texts: list[str], tag: str, ignored: Ignored, disabled: Iterable[str],
               suggest_limit: int = SUGGEST_LIMIT) -> LayerResult:
    if name == 'hunspell':
        return hunspell(texts, tag, ignored, suggest_limit)
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
          unsupported: dict[str, list[Snippet]] | None = None, suggest_limit: int = SUGGEST_LIMIT) -> Report:
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
            r = _run_layer(name, texts, tag, ignored, disabled_rules, suggest_limit)
            if r.note:
                rep.notes.append(f'[{name}] {tag}: {r.note}')
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
    # Not fully checked wins over findings: fixing what was found would not make the copy clean.
    if incomplete:
        rep.exit_code = EXIT_UNCHECKED
    elif rep.findings:
        rep.exit_code = EXIT_FINDINGS
    return rep


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='langcheck.py',
        description='Spelling and grammar gate for marketing copy (cs, sk, pl, de, en).',
        epilog='Exit codes: 0 clean, 1 findings, 2 bad input, 3 could not check fully (findings, if any, '
               'are still listed). Environment: LANGTOOLS_DICTS (dictionary folder), LT_URL (LanguageTool server).')
    p.add_argument('inputs', nargs='*', metavar='FILE',
                   help='files (.txt .md .html .json, anything else is read as text/Markdown) or "-" for stdin')
    p.add_argument('--text', action='append', default=[], metavar='COPY',
                   help='the copy itself (repeatable); a quoted positional argument still works but is deprecated')
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
    p.add_argument('--suggest-limit', type=int, default=SUGGEST_LIMIT, metavar='N',
                   help=f'Hunspell suggestions only for the first N unknown words per language (default '
                        f'{SUGGEST_LIMIT}); each takes seconds in pure Python')
    p.add_argument('--no-suggest', action='store_true', help='no Hunspell suggestions at all (fastest)')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--json-out', metavar='FILE', help='also write the machine-readable report to FILE')
    p.add_argument('--github-annotations', action='store_true',
                   help='also print GitHub Actions ::error lines, so findings show inline on a pull request')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return p


def _where(source: str, line: int, col: int) -> str:
    return f'{source}:{line}:{col}' if line else source


def is_legacy_text(arg: str) -> bool:
    """A positional argument that is copy, not a file (deprecated: use --text)."""
    return arg != '-' and not os.path.isfile(arg) and not looks_like_path(arg)


def gather(inputs: list[str], lang_arg: str = '', force_lang: str = '', all_strings: bool = False,
           texts: Iterable[str] = ()) -> tuple[dict[str, list[Snippet]], dict[str, list[Snippet]]]:
    """Return (language tag -> snippets, unsupported element-level language -> snippets)."""
    groups: dict[str, list[Snippet]] = {}
    unsupported: dict[str, list[Snippet]] = {}
    n_args = 0
    items = [(a, False) for a in inputs] + [(t, True) for t in texts]
    n_text = 0
    for arg, is_text in items:
        if is_text:
            n_text += 1
            src = f'<text{n_text}>'
            snippets = [s for s in [_plain(arg, src)] if s]
        elif arg == '-':
            src = '<stdin>'
            snippets = parse_text(sys.stdin.read(), src)
        elif os.path.isfile(arg):
            src = arg
            snippets = collect(arg, all_strings)
        elif looks_like_path(arg):
            raise UsageError(f'file not found: {arg} (copy that looks like a path goes in with --text)')
        else:
            n_args += 1
            src = f'<arg{n_args}>'
            snippets = [s for s in [_plain(arg, src)] if s]
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
    if rep.findings:
        lines.append(f'{len(rep.findings)} findings')
    if rep.exit_code == EXIT_UNCHECKED:
        missing = [f"{tag} ({', '.join(c['skipped'])})" for tag, c in rep.checked.items() if c['skipped']]
        missing += [f'lang="{lang}" (not supported)' for lang in rep.unsupported]
        hint = '' if not rep.strict else '; --lenient accepts this when every text was checked by at least one layer'
        lines.append('NOT CHECKED, did not run: ' + '; '.join(missing) + hint)
    elif not rep.findings:
        lines.append('clean')
    return '\n'.join(lines)


def _gh_escape(s: str, prop: bool = False) -> str:
    s = s.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    return s.replace(':', '%3A').replace(',', '%2C') if prop else s


def github_annotations(rep: Report) -> str:
    """GitHub Actions workflow commands: one ::error per finding, shown inline on the
    pull request diff, plus one for an incomplete check."""
    lines = []
    for f in rep.findings:
        props = [f'title={_gh_escape("copy-langcheck " + f.tool, True)}']
        if not f.source.startswith('<'):
            props.insert(0, f'file={_gh_escape(f.source, True)}')
            if f.line:
                props[1:1] = [f'line={f.line}', f'col={f.col}']
        sug = ', '.join(f.suggest) or '?'
        lines.append(f'::error {",".join(props)}::{_gh_escape(f"{f.found} -> {sug} ({f.message})")}')
    if rep.exit_code == EXIT_UNCHECKED:
        missing = [f"{tag} ({', '.join(c['skipped'])})" for tag, c in rep.checked.items() if c['skipped']]
        missing += [f'lang="{lang}" (not supported)' for lang in rep.unsupported]
        lines.append('::error title=copy-langcheck::' + _gh_escape('NOT CHECKED, did not run: ' + '; '.join(missing)))
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
    if not args.inputs and not args.text:
        print('langcheck: nothing to check; give files, "-" for stdin or --text "copy"', file=sys.stderr)
        return EXIT_USAGE
    if any(is_legacy_text(a) for a in args.inputs):
        print('langcheck: copy as a positional argument is deprecated and will be removed in 2.0; '
              'use --text "copy" (anything that looks like a path is now read as a file)', file=sys.stderr)
    try:
        lang_arg = norm_lang(args.lang) if args.lang else ''
        force = norm_lang(args.force_lang) if args.force_lang else ''
        groups, unsupported = gather(args.inputs, lang_arg, force, args.all_strings, args.text)
        default_ignore = os.path.join(HERE, 'ignore.txt')
        ignored = make_ignored(([default_ignore] if os.path.exists(default_ignore) else []) + args.ignore)
    except (UsageError, OSError, UnicodeDecodeError) as e:
        print(f'langcheck: {e}', file=sys.stderr)
        return EXIT_USAGE
    disabled = ignored.rules + [r.strip() for x in args.disable_rule for r in x.split(',') if r.strip()]
    rep = check(groups, ignored, online=args.online and not args.no_online, use_lt=not args.no_languagetool,
                strict=not args.lenient, disabled_rules=disabled, unsupported=unsupported,
                suggest_limit=0 if args.no_suggest else max(args.suggest_limit, 0))
    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as f:
            f.write(report_json(rep) + '\n')
    print(report_json(rep) if args.json else render(rep))
    if args.github_annotations:
        notes = github_annotations(rep)
        if notes:
            print(notes)
    return rep.exit_code


if __name__ == '__main__':
    sys.exit(main())
