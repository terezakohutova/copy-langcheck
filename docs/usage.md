# Usage reference

The [README](../README.md) covers what the tool does, a demo and the GitHub Action.
This page has the details: languages, input formats, options, exit codes, what
happens when a layer is missing, and speed.

## Languages

Language tags follow BCP 47; case and `_` or `-` do not matter.

| Tag | Dictionary | LanguageTool |
|---|---|---|
| `cs`, `cs-CZ` | cs_CZ | none |
| `sk`, `sk-SK` | sk_SK | sk-SK |
| `pl`, `pl-PL` | pl_PL | pl-PL |
| `de`, `de-DE` | de_DE_frami | de-DE |
| `de-AT` | de_AT_frami | de-AT |
| `de-CH` | de_CH_frami | de-CH |
| `en`, `en-GB` | en_GB | en-GB |
| `en-US` | en_US | en-US |

Anything else is exit 2 with a message, including country codes used as languages:
`cz` (Czech is `cs`), `uk` (that is Ukrainian; British English is `en-GB`), `at`,
`ch`, `gb`. A JSON `market` value is not read as a language, because a market is a
country.

Where the language comes from, strongest first:

1. `--force-lang` overrides everything.
2. The source itself: `<html lang>`, any element's `lang` (mixed-language pages are
   checked part by part), a JSON `lang` or `language` key, which applies to the
   object it is in and everything below it.
3. `--lang`, for texts that state nothing (plain text, Markdown, stdin, `--text`).

A part of a page marked with a language this tool does not support (`<q lang="fr">`)
is skipped and reported as not checked. A whole file in such a language is exit 2.

## Commands

```
python langcheck.py --lang cs --text "Pozvi kamaráda a získej odměnu"   # the copy itself
python langcheck.py --lang pl copy.txt                            # plain text or Markdown, one check per paragraph
python langcheck.py page.html ads.json                            # several files; each can state its own language
python langcheck.py --lang de-AT --all-strings strings.json       # every string value in any JSON
cat draft.md | python langcheck.py --lang en-US -                 # stdin
```

## Input formats

- **Files or copy:** every argument is a file, or `-` for stdin. Copy goes in with
  `--text` (repeatable). An argument without spaces that has a `/` or `\` or ends in
  something like an extension (`ads.yaml`, `copy/ads`, even `vis.zkus`) is a file,
  and a missing one is exit 2, so a typo in a path never passes as checked copy.
- **HTML:** visible text, with inline elements (`a`, `span`, `b`, `em`...) joined into
  their sentence and `<br>` read as a space; block elements (`p`, `li`, `div`, `h1`...)
  and form controls start a new text. Also `<title>`, `<meta name="description">`,
  `og:title`, `og:description`, `twitter:title`, `twitter:description`, and the
  attributes `alt`, `title`, `placeholder`, `aria-label`. Scripts, styles and SVG are
  skipped. Inline `code`, `kbd`, `samp` and anything marked `translate="no"` or
  `class="notranslate"` (brand names, product codes) become the placeholder `X`, so
  the sentence around them stays a sentence for LanguageTool; `translate="yes"`
  inside such an element is checked again. `pre` blocks leave nothing.
- **Markdown:** paragraphs, with links read as their text and inline code as the
  placeholder `X`. Code blocks, HTML comments and YAML front matter are skipped,
  except the front matter's `title` and `description`, which are copy.
- **JSON** without `--all-strings`: the keys `headline(s)`, `title`, `subtitle`,
  `sub`, `subline`, `cta`, `text`, `copy`, `body`, `primary_text`, `description(s)`,
  `long_headline`, `subject`, `preheader`, at any depth. Each object can carry its
  own `lang`.

## Options

- **Ignore lists:** `--ignore words.txt` (repeatable), plus `ignore.txt` next to the
  script. One entry per line, `#` for comments. Case-sensitive the way Hunspell is:
  `zorbly` also covers `Zorbly` and `ZORBLY`; `Zorbly` covers `ZORBLY` but not
  `zorbly`, so a brand written wrong is still caught. `i:word` matches in any case.
  `rule:RULE_ID` switches off a LanguageTool rule. See
  [`examples/ignore.example.txt`](../examples/ignore.example.txt).
- **LanguageTool rules:** `--disable-rule ID` (repeatable or comma-separated) on top
  of the formatting rules that are already off (missing full stop on a headline,
  typographic quotes and the like).
- **Capitals:** words in capitals are checked (`STÁHNOUT ZDRAMA` fails on `ZDRAMA`).
  Skipped: tokens of up to 4 characters with a digit (`B2B`, `4K`), numbers, e-mail
  addresses, URLs, `www.` hosts and bare domains with a known top-level domain
  (`example.cz`). A missing space after a full stop (`vis.zkus`) is not a domain, so
  both words are checked. A few acronyms (`API`, `SMS`, `PDF`, `EUR`...) are built in;
  add your own to an ignore list.
- `--json`: findings with tool, word, suggestions, rule, message, the text, file,
  line, column and offset, plus which layers ran and which did not and why.
  `--json-out FILE` writes the same next to the normal output.
- `--github-annotations`: also print `::error` workflow commands, so findings appear
  on their line in a pull request.
- `--online` adds Korektor for Czech (all texts of a run go in as few requests as
  possible). See the Korektor terms in the README before using it on commercial copy.
- `--no-suggest` skips Hunspell suggestions; `--suggest-limit N` (default 20) caps
  them per language. Findings are the same either way.
- `LANGTOOLS_DICTS` and `LT_URL` move the dictionaries and the server.

## Exit codes

| Exit code | Meaning |
|---|---|
| 0 | clean |
| 1 | findings, and every layer that applies checked every text |
| 2 | bad input: no or unsupported language, file not found, not UTF-8, invalid JSON, a file with no copy in it |
| 3 | could not check fully: a layer that applies did not check every text (with `--lenient`: a text was checked by no layer). Findings, if any, are still listed and in the JSON report |

3 wins over 1: if LanguageTool was down and Hunspell found a typo, the result is 3,
because fixing the typo would not make the copy clean. With `--lenient`, findings
give 1 as long as every text was checked by at least one layer.

## When a layer is missing

A missing layer never passes silently. If LanguageTool is down, the result for
Slovak, Polish, German or English is exit 3, not "clean":

```
$ python langcheck.py --lang sk examples/clean-sk.txt
[languagetool] sk-SK: http://localhost:8081/v2/check: not reachable (...), grammar NOT checked
2 texts checked in sk-SK by hunspell
NOT CHECKED, did not run: sk-SK (languagetool); --lenient accepts this when every text was checked by at least one layer
```

- `--no-languagetool` says you don't want grammar; then spelling alone can give exit 0.
- `--lenient` accepts a missing layer as long as every text was checked by at least
  one; the note stays in the output.
- A failed LanguageTool or Korektor request is retried once (network errors,
  timeouts and 5xx only; a 4xx such as a rate limit is not). Texts that still fail
  are listed as not checked (`2 of 3 texts not checked`), findings from the texts
  before are kept, and the run ends with exit 3. When the server is gone altogether,
  texts not sent yet are not sent at all.
- LanguageTool gets up to 4 texts at once, and every failure is counted in the note
  (`HTTP 500 (2 texts)`).
- If Korektor does not return a batch one to one, each text is sent on its own, at
  most 20 requests per run; the rest are listed as not checked.
- Without dictionaries and without a server the result is exit 3 even with
  `--lenient`.

## Speed

spylls is pure Python. A dictionary loads once per run (about 1 to 7 s), so pass all
files of a job to one call. The slow part is suggestions for unknown words, a few
seconds each, so only the first 20 per language get them by default and the Action
skips them. `--no-suggest` is the fastest gate: on a Windows laptop the Czech
example (3 unknown words) took about 11 s with suggestions and 5 to 7 s without,
most of it the dictionary load.

## spylls

Hunspell runs through [spylls](https://github.com/zverok/spylls), a pure-Python port,
so nothing needs compiling on any OS. It is pinned to `spylls==0.1.7` with hashes in
[`requirements.txt`](../requirements.txt). Checked on 2026-10-13: 0.1.7 is the last
release (January 2022) and the repository's last push was in March 2024, so treat it
as finished rather than maintained. It has no dependencies, and CI runs it on Python
3.10 and 3.13 on three OSes, so a break shows up there first. If it stops working on
a new Python, the plan is, in this order: pin the Python version in CI and the
Action; vendor the pinned 0.1.7 source (MPL 2.0, which allows that with the licence
notice kept); or switch the Hunspell layer to the `hunspell` binary, which reads the
same dictionaries.

## CI of this repository

[`tests.yml`](../.github/workflows/tests.yml) runs the unit tests on Linux, macOS and
Windows with Python 3.10 and 3.13, plus ruff. [`selftest.yml`](../.github/workflows/selftest.yml)
runs the Action itself with a real LanguageTool container and checks exact exit
codes: clean Slovak copy gives 0 in strict mode, the planted mistakes in `examples/`
give 1, a grammar-only mistake is reported by LanguageTool alone (read from the JSON
report), a missing file gives 2, and a Windows runner asked for the container fails.
[`korektor-smoke.yml`](../.github/workflows/korektor-smoke.yml) runs only when started
by hand: it sends the public example copy to the real Korektor API, because it
depends on a third-party service.
