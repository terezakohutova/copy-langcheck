# -*- coding: utf-8 -*-
"""Spelling and grammar check for marketing copy in Czech, Slovak, Polish, German and English.

Read-only: it never changes your text. Exit code 1 when anything is found, so it
can gate a build, a commit or an ad upload.

    python langcheck.py --lang cs "Pozvi kamaráda a získej odměnu"
    python langcheck.py --lang pl copy.txt              # plain text or Markdown, one check per paragraph
    python langcheck.py page.html                       # visible text + alt/title/placeholder, language from <html lang>
    python langcheck.py ads.json                        # {"lang": "sk", "headline": "...", "copy": ["...", ...]}
    python langcheck.py --lang de --all-strings app-strings.json   # every string value in the JSON
    options: --no-online (skip Korektor), --json (machine-readable output)

Tools, each optional (a missing one is reported, never fatal):
  1. Hunspell via spylls + LibreOffice dictionaries, fully local. Get the
     dictionaries with: python fetch_dicts.py
  2. LanguageTool, a local server (LT_URL, default http://localhost:8081/v2/check).
     Grammar, agreement, punctuation. It has no Czech module.
  3. Korektor (UFAL, Charles University) for Czech spelling and diacritics, via the
     public LINDAT API. The only tool that sends text out; --no-online turns it off.
Words that are fine on purpose (brand, product and feature names): one per line in
ignore.txt next to this script, or in a file passed with --ignore.
"""
import html.parser
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DICTS = os.environ.get('LANGTOOLS_DICTS') or os.path.join(HERE, 'dicts')
LT_URL = os.environ.get('LT_URL') or 'http://localhost:8081/v2/check'
KOREKTOR = 'https://lindat.mff.cuni.cz/services/korektor/api/correct'

ALIASES = {'cz': 'cs', 'cs-cz': 'cs', 'sk-sk': 'sk', 'pl-pl': 'pl', 'de-de': 'de', 'at': 'de',
           'de-at': 'de', 'ch': 'de', 'de-ch': 'de', 'en-gb': 'en', 'en-us': 'en', 'uk': 'en', 'gb': 'en'}
LT_LANG = {'en': 'en-GB', 'de': 'de-DE', 'pl': 'pl-PL', 'sk': 'sk-SK'}
HUNSPELL = {'cs': 'cs_CZ', 'sk': 'sk_SK', 'pl': 'pl_PL', 'de': 'de_DE_frami', 'en': 'en_GB'}

# Platform, tech and currency words that are never typos in marketing copy.
IGNORE = {
    'iOS', 'Android', 'App', 'Store', 'Google', 'Play', 'Apple', 'Pay', 'Meta', 'TikTok', 'Instagram',
    'Facebook', 'YouTube', 'WhatsApp', 'LinkedIn', 'Visa', 'Mastercard', 'PayPal', 'API', 'CTA',
    'EUR', 'USD', 'CZK', 'PLN', 'Kč', 'zł', 'app', 'apps', 'appka', 'appku', 'appce', 'appky',
    'cashback', 'online', 'e-mail', 'email', 'newsletter', 'onboarding', 'fintech', 'premium', 'www',
}

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


def norm_lang(x):
    x = (x or '').strip().lower()
    return ALIASES.get(x, x.split('-')[0] if x else '')


def load_ignore(extra):
    for p in [os.path.join(HERE, 'ignore.txt')] + ([extra] if extra else []):
        if p and os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                IGNORE.update(w.strip() for w in f if w.strip() and not w.lstrip().startswith('#'))
    return {w.lower() for w in IGNORE}


class _HTML(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang, self.out, self.buf, self.skip = '', [], [], 0

    def flush(self):
        t = re.sub(r'\s+', ' ', ''.join(self.buf)).strip()
        if t:
            self.out.append(t)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'html' and a.get('lang'):
            self.lang = a['lang']
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.flush()
        for k in ('alt', 'title', 'placeholder', 'aria-label'):
            if a.get(k) and not self.skip:
                self.out.append(a[k].strip())

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, d):
        if not self.skip:
            self.buf.append(d)


def collect(path, all_strings):
    """Return (lang or '', [texts]) from a .json, .html or text/Markdown file."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    if ext == '.json':
        d = json.loads(raw)
        texts = []

        def walk(x, key=''):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, k)
            elif isinstance(x, list):
                for v in x:
                    walk(v, key)
            elif isinstance(x, str) and (all_strings or key in TEXT_KEYS):
                texts.append(x)
        walk(d)
        lang = d.get('lang') or d.get('language') or d.get('market') if isinstance(d, dict) else ''
        return norm_lang(lang), texts
    if ext in ('.html', '.htm'):
        p = _HTML()
        p.feed(raw)
        p.flush()
        return norm_lang(p.lang), p.out
    text = re.sub(r'```.*?```', ' ', raw, flags=re.S)                    # Markdown code blocks
    text = re.sub(r'`[^`]*`|!\[[^\]]*\]\([^)]*\)|<[^>]+>', ' ', text)    # inline code, images, tags
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)                  # links -> their text
    text = re.sub(r'^[ \t]{0,3}(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+|>[ \t]?)', '', text, flags=re.M)
    text = text.replace('**', '').replace('__', '')
    return '', [re.sub(r'\s+', ' ', p).strip() for p in re.split(r'\n\s*\n', text) if p.strip()]


def unknown_words(texts, lang, ignored):
    name = HUNSPELL.get(lang)
    if not name or not os.path.exists(os.path.join(DICTS, name + '.aff')):
        return None, f'no {name or lang} dictionary in {DICTS} (run: python fetch_dicts.py)'
    try:
        from spylls.hunspell import Dictionary
    except ImportError:
        return None, 'spylls not installed (pip install spylls)'
    d = Dictionary.from_files(os.path.join(DICTS, name))
    out = []
    seen = set()
    for t in texts:
        clean = re.sub(r'\S+@\S+|https?://\S+|\b[\w.-]+\.[a-z]{2,4}\b\S*', ' ', t)
        for w in re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", clean):
            if w in seen or len(w) < 2 or ignored(w) or (w.isupper() and len(w) <= 6):
                continue
            ok = d.lookup(w) or (w[0].isupper() and d.lookup(w.lower()))
            if not ok and re.search(r"['’]", w):
                ok = all(d.lookup(p) or ignored(p) for p in re.split(r"['’]", w) if len(p) > 1)
            if not ok:
                seen.add(w)
                sug = []
                try:
                    sug = list(d.suggest(w))[:3]
                except Exception:
                    pass
                out.append({'tool': 'hunspell', 'found': w, 'suggest': sug, 'message': 'unknown word', 'text': t})
    return out, None


def languagetool(texts, lang, ignored):
    ltl = LT_LANG.get(lang)
    if not ltl:
        return [], None
    out = []
    try:
        for t in texts:
            data = urllib.parse.urlencode({'text': t, 'language': ltl, 'level': 'picky',
                                           'disabledRules': LT_DISABLED}).encode()
            with urllib.request.urlopen(LT_URL, data=data, timeout=120) as r:
                for m in json.load(r)['matches']:
                    bad = t[m['offset']:m['offset'] + m['length']]
                    if ignored(bad):
                        continue
                    out.append({'tool': 'languagetool', 'found': bad, 'rule': m['rule']['id'],
                                'suggest': [x['value'] for x in m['replacements'][:3]],
                                'message': m['message'], 'text': t})
    except Exception as e:
        return out, f'not reachable at {LT_URL} ({e.__class__.__name__}), grammar NOT checked'
    return out, None


def korektor(texts, ignored):
    out = []
    try:
        for t in texts:
            q = urllib.parse.urlencode({'model': 'czech-spellchecker-130202', 'data': t})
            with urllib.request.urlopen(KOREKTOR + '?' + q, timeout=30) as r:
                fixed = json.load(r)['result']
            a, b = re.findall(r'\S+', t), re.findall(r'\S+', fixed)
            pairs = list(zip(a, b)) if len(a) == len(b) else ([(t, fixed)] if fixed.strip() != t.strip() else [])
            for x, y in pairs:
                if x != y and not ignored(x.strip('.,!?:;"()')):
                    out.append({'tool': 'korektor', 'found': x, 'suggest': [y], 'message': 'spelling/diacritics', 'text': t})
    except Exception as e:
        return out, f'not reachable ({e.__class__.__name__}), Czech checked by dictionary only'
    return out, None


def main(argv):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    flags = {a for a in argv if a in ('--no-online', '--json', '--all-strings')}
    args = [a for a in argv if a not in flags]
    lang, ignore_file = '', None
    if '--lang' in args:
        i = args.index('--lang')
        lang = norm_lang(args[i + 1])
        del args[i:i + 2]
    if '--ignore' in args:
        i = args.index('--ignore')
        ignore_file = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    texts = []
    if len(args) == 1 and os.path.isfile(args[0]):
        flang, texts = collect(args[0], '--all-strings' in flags)
        lang = lang or flang
    else:
        texts = args
    if not lang:
        print('cannot tell the language; pass --lang cs|sk|pl|de|en')
        return 2
    lower = load_ignore(ignore_file)

    def ignored(w):
        return w in IGNORE or w.lower() in lower

    texts = [t for t in dict.fromkeys(texts) if t.strip()]
    findings, notes = [], []
    for name, (res, note) in (('hunspell', unknown_words(texts, lang, ignored)),
                              ('languagetool', languagetool(texts, lang, ignored))):
        findings += res or []
        if note:
            notes.append(f'[{name}] {note}')
    if lang == 'cs':
        if '--no-online' in flags:
            notes.append('[korektor] skipped (--no-online)')
        else:
            res, note = korektor(texts, ignored)
            findings += res
            if note:
                notes.append(f'[korektor] {note}')

    if '--json' in flags:
        print(json.dumps({'lang': lang, 'texts': len(texts), 'findings': findings, 'notes': notes},
                         ensure_ascii=False, indent=1))
    else:
        for n in notes:
            print(n)
        for f in findings:
            sug = ', '.join(f.get('suggest') or []) or '?'
            rule = f" {f['rule']}" if f.get('rule') else ''
            ctx = f['text'] if len(f['text']) <= 120 else f['text'][:117] + '...'
            print(f"[{f['tool']}{rule}] \"{f['found']}\" -> {sug}  ({f['message']})  in: {ctx}")
        print(f'{len(texts)} texts checked in {lang}: ' + (f'{len(findings)} findings' if findings else 'clean'))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
