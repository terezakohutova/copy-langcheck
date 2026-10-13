# copy-langcheck

[![CI](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml/badge.svg)](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml)

Spelling and grammar gate for marketing copy in **Czech, Slovak, Polish, German and English** (grammar for SK/PL/DE/EN via LanguageTool; Czech spelling, grammar via optional Korektor): ad texts, landing pages, newsletters, app strings. One Python file, read-only, standard library plus one pure-Python dependency. Exit code 1 when it finds something, so it can sit in front of an upload, a build or a commit.

Most copy tools are built for English. For Czech and Slovak there is very little you can run locally and script, so this puts together three open tools that are good at different things:

| Layer | Catches | Languages | Where it runs |
|---|---|---|---|
| **Hunspell** ([spylls](https://github.com/zverok/spylls) + [LibreOffice dictionaries](https://github.com/LibreOffice/dictionaries)) | unknown words, missing diacritics, typos | cs, sk, pl, de-DE, de-AT, de-CH, en-GB, en-US | locally, always |
| **[LanguageTool](https://github.com/languagetool-org/languagetool)** | grammar, agreement, cases, repeated words, commas | the same minus Czech (no Czech module) | a server you run |
| **[Korektor](https://ufal.mff.cuni.cz/korektor)** (UFAL, Charles University) | Czech spelling and diacritics in context | cs | public LINDAT API, **off unless `--online`** (see [Korektor terms](#korektor-terms)) |

When two layers flag the same word, it is one finding that lists both (`[hunspell +languagetool MORFOLOGIK_RULE_PL_PL]`).

## Try it in a minute

```
python -m pip install -r requirements.txt
python fetch_dicts.py cs
python langcheck.py --no-languagetool examples/ad-copy.json
```

```
examples/ad-copy.json:3:21: [hunspell] "kamarada" -> kamaráda, Kamrada, kamarádka  (unknown word)  in: Pozvi kamarada a ziskej odmenu
examples/ad-copy.json:3:32: [hunspell] "ziskej" -> získej, ziskem  (unknown word)  in: Pozvi kamarada a ziskej odmenu
examples/ad-copy.json:3:39: [hunspell] "odmenu" -> odměnu, podmenu, odmetu  (unknown word)  in: Pozvi kamarada a ziskej odmenu
4 texts checked in cs-CZ by hunspell
3 findings
```

Exit code 1. Each finding starts with `file:line:column`, so editors and CI logs can jump to it. Czech has no LanguageTool module, so `--no-languagetool` changes nothing here; for the other languages, see [When a layer is missing](#when-a-layer-is-missing).

With a LanguageTool server, grammar is checked too. Every word in [`examples/grammar-de.txt`](examples/grammar-de.txt) is in the dictionary, so only LanguageTool can catch these (messages shortened):

```
$ python langcheck.py --lang de examples/grammar-de.txt
examples/grammar-de.txt:1:9: [languagetool DE_AGREEMENT] "ein gute Zeichen" -> ein gutes Zeichen  (Möglicherweise passen das Nomen und die Wörter, ...)
examples/grammar-de.txt:3:5: [languagetool PRAEP_DAT] "die" -> der  (Die Präposition „Mit“ erwartet den Dativ. ...)
2 texts checked in de-DE by hunspell, languagetool
2 findings
```

Unedited outputs for all five examples (Czech ad copy as JSON, a Polish landing page as HTML, German newsletter and grammar text, clean Slovak text) are in [`examples/output/`](examples/output/), each with the command and the LanguageTool server it ran against. They were captured against the **public LanguageTool API** (`https://api.languagetool.org/v2/check`) and, for the Czech one, the public Korektor API, because the example copy is public anyway. Use your own server for real copy.

## Setup

Python 3.10 or newer, on Linux, macOS or Windows (CI runs all three).

```
python -m pip install -r requirements.txt   # spylls, pure Python
python fetch_dicts.py                       # all languages and variants into ./dicts
python fetch_dicts.py cs sk en-US           # or only some
```

`fetch_dicts.py` downloads from a pinned commit of the LibreOffice repository, so every machine gets the same dictionaries; `--ref master` takes the newest instead. That is enough for spelling. For grammar, run a LanguageTool server and point `LT_URL` at it (default `http://localhost:8081/v2/check`):

- **Docker (Linux containers):** `docker run -d -p 8081:8010 erikvl87/languagetool:6.8`
- **Without Docker:** download the [LanguageTool desktop/server zip](https://languagetool.org/download/), then with Java 17+: `java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081`

The public LanguageTool API also works (`LT_URL=https://api.languagetool.org/v2/check`), but it has rate limits and sends your text to a third party. Don't use it for anything unpublished.

## Languages

Language tags follow BCP 47, case and `_` or `-` do not matter:

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

Anything else is exit 2 with a message, including country codes used as languages: `cz` (Czech is `cs`), `uk` (that is Ukrainian; British English is `en-GB`), `at`, `ch`, `gb`. A JSON `market` value is not read as a language, because a market is a country.

Where the language comes from, strongest first:

1. `--force-lang` overrides everything.
2. The source itself: `<html lang>`, any element's `lang` (mixed-language pages are checked part by part), a JSON `lang` or `language` key, which applies to the object it is in and everything below it.
3. `--lang`, for texts that state nothing (plain text, Markdown, stdin, quoted copy).

A part of a page marked with a language this tool does not support (`<q lang="fr">`) is skipped and reported as not checked. A whole file in such a language is exit 2.

## Usage

```
python langcheck.py --lang cs --text "Pozvi kamaráda a získej odměnu"   # the copy itself
python langcheck.py --lang pl copy.txt                            # plain text or Markdown, one check per paragraph
python langcheck.py page.html ads.json                            # several files; each can state its own language
python langcheck.py --lang de-AT --all-strings strings.json       # every string value in any JSON
cat draft.md | python langcheck.py --lang en-US -                 # stdin
```

- **Files or copy:** every argument is a file, or `-` for stdin. Copy goes in with `--text` (repeatable). An argument without spaces that has a `/` or `\` or ends in something like an extension (`ads.yaml`, `copy/ads`, even `vis.zkus`) is a file, and a missing one is exit 2, so a typo in a path never passes as checked copy. Quoted copy as a plain argument still works but prints a deprecation notice and goes away in 2.0.
- **HTML:** visible text, with inline elements (`a`, `span`, `b`, `em`...) joined into their sentence and `<br>` read as a space; only block elements (`p`, `li`, `div`, `h1`...) and form controls start a new text. Also `<title>`, `<meta name="description">`, `og:title`, `og:description`, `twitter:title`, `twitter:description`, and the attributes `alt`, `title`, `placeholder`, `aria-label`. Scripts, styles and SVG are skipped. Inline `code`, `kbd`, `samp` and anything marked `translate="no"` or `class="notranslate"` (brand names, product codes) are replaced by the placeholder `X`, so the sentence around them stays a sentence for LanguageTool; `translate="yes"` inside such an element is checked again. Blocks of code (`pre`) leave nothing.
- **Markdown:** paragraphs, with links read as their text and inline code as the placeholder `X`. Code blocks, HTML comments (also over several lines) and YAML front matter are skipped, except the front matter's `title` and `description`, which are copy.
- **JSON** without `--all-strings`: the keys `headline(s)`, `title`, `subtitle`, `sub`, `subline`, `cta`, `text`, `copy`, `body`, `primary_text`, `description(s)`, `long_headline`, `subject`, `preheader`, at any depth. Each object can carry its own `lang`.
- **Ignore lists:** `--ignore words.txt` (repeatable), plus `ignore.txt` next to the script. One entry per line, `#` for comments. Case-sensitive the way Hunspell is: `zorbly` also covers `Zorbly` and `ZORBLY`; `Zorbly` covers `ZORBLY` but not `zorbly`, so a brand written wrong is still caught. `i:word` matches in any case. `rule:RULE_ID` switches off a LanguageTool rule. See [`examples/ignore.example.txt`](examples/ignore.example.txt).
- **LanguageTool rules:** `--disable-rule ID` (repeatable or comma-separated) switches off a rule on top of the formatting rules that are already off (missing full stop on a headline, typographic quotes and the like).
- **Capitals:** words in capitals are checked (`STÁHNOUT ZDRAMA` fails on `ZDRAMA`). Skipped: tokens of up to 4 characters with a digit (`B2B`, `4K`), numbers, e-mail addresses, URLs, `www.` hosts and bare domains with a known top-level domain (`example.cz`). A missing space after a full stop (`vis.zkus`) is not a domain, so both words are checked. Short acronyms that are fine go in your ignore list; a few (`API`, `SMS`, `PDF`, `EUR`...) are built in.
- `--json`: findings with tool, word, suggestions, rule, message, the text, file, line, column and offset, plus which layers ran and which did not and why. `--json-out FILE` writes the same next to the normal output.
- `--online` adds Korektor for Czech (all texts of a run go in as few requests as possible). `--no-online` still works but is deprecated: off is the default.
- `LANGTOOLS_DICTS` and `LT_URL` move the dictionaries and the server.

| Exit code | Meaning |
|---|---|
| 0 | clean |
| 1 | findings, and every layer that applies checked every text |
| 2 | bad input: no or unsupported language, file not found, not UTF-8, invalid JSON, a file with no copy in it |
| 3 | could not check fully: a layer that applies did not check every text (with `--lenient`: a text was checked by no layer). Findings, if any, are still listed and in the JSON report |

Since 1.3.0, 3 wins over 1: if LanguageTool was down and Hunspell found a typo, the result is 3, not 1, because fixing the typo would not make the copy clean. With `--lenient`, findings give 1 as long as every text was checked by at least one layer.

### When a layer is missing

A missing layer never passes silently. Since 1.2.0 the default is strict: if LanguageTool is down, the result for Slovak, Polish, German or English is exit 3, not "clean". Without a server (Windows error text shortened):

```
$ python langcheck.py --lang sk examples/clean-sk.txt
[languagetool] sk-SK: http://localhost:8081/v2/check: not reachable (...), grammar NOT checked
2 texts checked in sk-SK by hunspell
NOT CHECKED, did not run: sk-SK (languagetool); --lenient accepts this when every text was checked by at least one layer
```

- `--no-languagetool` says you don't want grammar; then spelling alone can give exit 0.
- `--lenient` accepts a missing layer as long as every text was checked by at least one; the note stays in the output.
- If LanguageTool or Korektor fails part way through a run, each failed request is retried once. Texts that still fail are listed as not checked (`2 of 3 texts not checked`), findings from the texts before are kept, and the run ends with exit 3. Only network errors, timeouts and 5xx answers are retried; a 4xx (bad request, rate limit) is not. When the server is gone altogether, texts not sent yet are not sent at all.
- LanguageTool gets up to 4 texts at once, and every failure is counted in the note (`HTTP 500 (2 texts)`).
- If Korektor does not return a batch one to one, each text is sent on its own, at most 20 requests per run; the rest are listed as not checked.
- Without dictionaries and without a server the result is exit 3 even with `--lenient`; a mistyped path is exit 2, not a check of the path as if it were text.

### Speed

spylls is pure Python. Loading a dictionary happens once per run, so pass all files of a job to one call. The slow part is **suggestions for unknown words**: each one is a search through the dictionary. Measured on one Windows laptop (Intel 11th gen, Python 3.14), in two sessions a day apart:

| | Czech | German | English |
|---|---|---|---|
| suggestions per unknown word, session 1 (5 words each) | 1.4 to 4.0 s | 2.8 to 6.0 s | 0.3 to 1.1 s |
| suggestions per unknown word, session 2 (3 Czech words) | 4.6 to 16.2 s | | |
| dictionary load, session 1 / session 2 | 1.7 s / 6.5 to 7.3 s | 2.1 s | 0.8 s |

The whole `examples/ad-copy.json` run (3 unknown Czech words) took 39.2 s with suggestions and 9.3 s with `--no-suggest` in session 2. So by default only the first 20 unknown words per language get suggestions (`--suggest-limit N` to change it); the rest are still reported, without suggestions, and a note says how many. Each word is looked up once per run. `--no-suggest` turns suggestions off for the fastest gate.

## GitHub Action

Fail a pull request when the copy has a mistake:

```yaml
# .github/workflows/copy.yml
name: copy
on: [pull_request]
jobs:
  langcheck:
    runs-on: ubuntu-latest   # the LanguageTool container needs a Linux runner
    steps:
      - uses: actions/checkout@v5
      - uses: terezakohutova/copy-langcheck@v1   # this repository
        with:
          files: copy/*.json copy/sk-landing.html   # or one path per line
          lang: ''            # fallback for files that state no language
          languagetool: true  # starts a pinned LanguageTool container on the runner
          strict: true        # the default: exit 3 if LanguageTool did not come up
          online: false       # true = also Korektor for Czech, text leaves the runner
          ignore: copy/brand-words.txt
```

- Outputs: `exit-code`, and `report`, the path of the JSON report (findings with file, line and column).
- Python: the action uses the job's Python if it is 3.10 or newer and only sets one up when there is none. spylls is installed, checked against pinned hashes, into a private folder under `RUNNER_TEMP`, not into your environment.
- LanguageTool: if a healthy server already answers on `localhost:8081` (a `services:` container, or one you started), the action uses it. Otherwise it starts the image `erikvl87/languagetool:6.8`, pinned by digest, under a name unique to the run, attempt, step and inputs, on a port Docker picks, and removes it at the end of the step whatever the outcome ([`scripts/languagetool.sh`](scripts/languagetool.sh); composite actions have no post step, so a `trap` does it). Using the action twice in one job, or two jobs on one self-hosted runner, no longer collide. The container needs Docker with Linux containers, so on macOS and Windows runners the step fails with a clear error when `strict` is true. There, set `lt-url` to a server you run, `languagetool: false`, or `strict: false` (grammar is then skipped with a warning).
- More inputs: `force-lang`, `all-strings`, `disable-rules`.

A third workflow, [`korektor-smoke.yml`](.github/workflows/korektor-smoke.yml), runs only when started by hand: it sends the public example copy to the real Korektor API and checks that Korektor ran and flagged the planted Czech mistakes. It is not part of every push because it depends on a third-party service.

Two workflows test this repository on every push: [`tests.yml`](.github/workflows/tests.yml) runs the unit tests on Linux, macOS and Windows with Python 3.10 and 3.13, plus ruff; [`selftest.yml`](.github/workflows/selftest.yml) runs the action itself with a real LanguageTool container and checks exact exit codes: clean Slovak copy must give 0 in strict mode, the planted mistakes in `examples/` must give 1, a grammar-only mistake must be reported by LanguageTool alone (read from the JSON report), a missing file must give 2, and a Windows runner asked for the container must fail.

## Use it as a gate locally

```
python langcheck.py --lang cs ad-copy.json || { echo "fix the copy first"; exit 1; }
```

In an agent setup, make "langcheck exit 0" a required step before the agent may hand over or upload copy.

## Tests

```
python -m unittest -v
```

The unit tests run offline: tiny fixture dictionaries stand in for the LibreOffice ones and LanguageTool and Korektor are mocked, including their failure modes. If `./dicts` exists, the bundled examples are also checked against the real dictionaries.

## spylls

Hunspell runs through [spylls](https://github.com/zverok/spylls), a pure-Python port, so nothing needs compiling on any OS. It is pinned to `spylls==0.1.7` with hashes in [`requirements.txt`](requirements.txt). Its maintenance status, checked on 2026-10-13: 0.1.7 is the last release, from January 2022, and the repository's last push was in March 2024, so treat it as finished rather than maintained. It has no dependencies, and CI runs it on Python 3.10 and 3.13 on three OSes, so a break shows up there first. If it stops working on a new Python, the plan is, in this order: pin the Python version in CI and the Action; vendor the pinned 0.1.7 source into this repository (MPL 2.0 according to its repository, which allows that with the licence notice kept); or switch the Hunspell layer to the `hunspell` binary, which reads the same dictionaries.

## Korektor terms

Korektor's code is BSD-licensed, but the models behind the public LINDAT service are **CC BY-NC-SA**, and the service page says that commercial use needs explicit written permission of the authors and that submitted text may be kept to improve their systems. Checking commercial marketing copy through it is therefore off by default. Turn it on with `--online` (or `online: true` in the Action) only if your use is non-commercial or you have that permission. Czech is still checked by Hunspell without it.

## Limits, honestly

- **A wrong word that is also a real word passes.** Slovak `prehlad` instead of `prehľad` is in the dictionary, so Hunspell accepts it, and LanguageTool did not flag it in testing either. Read the copy yourself.
- **Korektor's suggestion can be wrong even when the flag is right.** In the Czech example it correctly flags `ziskej` but suggests `ziskem`; Hunspell suggests the right `získej`. Treat suggestions as hints.
- **Positions point at the word in the source, not at a guess.** Every character of the checked text keeps its offset in the file through the extraction (HTML entities, JSON escapes, skipped markup), so a word that also appears in an `href` or inline code is reported where it is copy. A character that came from an entity or escape points at the start of that entity.
- **No tone, truth or brand rules.** The tools don't know your tone of voice, your banned claims or whether a number is true.
- **Short headlines without a verb** can trip grammar rules. Formatting-only rules (missing full stop, typographic quotes) are already switched off; switch off more with `--disable-rule`.
- **Czech has no grammar layer here**, only spelling (Hunspell, plus Korektor if you may use it).

## Licence

The code is MIT, see [LICENSE](LICENSE). The dictionaries are downloaded from the LibreOffice repository by `fetch_dicts.py` and keep their own licences (MPL, LGPL, GPL and others, see the README in each folder there); they are not redistributed here. The tiny dictionaries in `tests/fixtures/` are written for the tests. LanguageTool is LGPL. Korektor: see [Korektor terms](#korektor-terms). Changes per version: [CHANGELOG.md](CHANGELOG.md).

---

### Česky

Kontrola pravopisu a gramatiky pro marketingové texty v češtině, slovenštině, polštině, němčině (de-DE, de-AT, de-CH) a angličtině (en-GB, en-US). Spojuje Hunspell (lokálně, slovníky LibreOffice), LanguageTool (vlastní server, gramatika pro sk, pl, de, en) a volitelně Korektor z ÚFAL MFF UK (čeština, veřejné API; modely mají licenci CC BY-NC-SA, proto je ve výchozím stavu vypnutý). Nic nepřepisuje, jen hlásí, u každého nálezu se souborem, řádkem a sloupcem: exit 1 při nálezu, exit 3 když některá vrstva nemohla zkontrolovat všechno (například neběží LanguageTool), takže může hlídat texty před nahráním reklamy nebo před vydáním webu. Jazyk se zadává jako BCP 47 (`cs`, ne `cz`). Instalace: `python -m pip install -r requirements.txt` a `python fetch_dicts.py`. Na tón, pravdivost a značku se dívá pořád člověk.
