# copy-langcheck

[![CI](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml/badge.svg)](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml)

Spelling and grammar gate for marketing copy in **Czech, Slovak, Polish, German and English**: ad texts, landing pages, newsletters, app strings. One Python file, read-only, standard library plus one pure-Python dependency. Exit code 1 when it finds something, so it can sit in front of an upload, a build or a commit.

Most copy tools are built for English. For Czech and Slovak there is very little you can run locally and script, so this puts together three open tools that are good at different things:

| Layer | Catches | Languages | Where it runs |
|---|---|---|---|
| **Hunspell** ([spylls](https://github.com/zverok/spylls) + [LibreOffice dictionaries](https://github.com/LibreOffice/dictionaries)) | unknown words, missing diacritics, typos | cs, sk, pl, de, en | locally, always |
| **[LanguageTool](https://github.com/languagetool-org/languagetool)** | grammar, agreement, cases, repeated words, commas | sk, pl, de, en (no Czech module) | a server you run |
| **[Korektor](https://ufal.mff.cuni.cz/korektor)** (UFAL, Charles University) | Czech spelling and diacritics in context | cs | public LINDAT API, **off unless `--online`** (see [Korektor terms](#korektor-terms)) |

## Try it in a minute

```
python -m pip install -r requirements.txt
python fetch_dicts.py cs
python langcheck.py examples/ad-copy.json
```

```
[hunspell] "kamarada" -> kamaráda, Kamrada, kamarádka  (unknown word)  in: Pozvi kamarada a ziskej odmenu
[hunspell] "ziskej" -> získej, ziskem  (unknown word)  in: Pozvi kamarada a ziskej odmenu
[hunspell] "odmenu" -> odměnu, podmenu, odmetu  (unknown word)  in: Pozvi kamarada a ziskej odmenu
4 texts checked in cs by hunspell
3 findings
```

Exit code 1. That is the whole Hunspell layer, no server and nothing sent out. With a LanguageTool server the German newsletter example also gets its grammar checked (long lines shortened here):

```
$ python langcheck.py --lang de examples/newsletter-de.md
[hunspell] "Weihnachen" -> Weihnachten, Weihsachen, Weihaachen  (unknown word)  in: Neu: Mit die Sparziel-Funktion legst du fest, ...
[languagetool DE_AGREEMENT] "ein gute Zeichen" -> ein gutes Zeichen  (Möglicherweise passen das Nomen und die Wörter, ...)
[languagetool PRAEP_DAT] "die" -> der  (Die Präposition „Mit“ erwartet den Dativ. ...)
4 texts checked in de by hunspell, languagetool
3 findings
```

Unedited outputs for all four examples (Czech ad copy as JSON, a Polish landing page as HTML, a German newsletter as Markdown, clean Slovak text) are in [`examples/output/`](examples/output/), each with the command and the LanguageTool server it ran against.

## Setup

Python 3.10 or newer, on Linux, macOS or Windows (CI runs all three).

```
python -m pip install -r requirements.txt   # spylls, pure Python
python fetch_dicts.py                       # cs sk pl de en into ./dicts (or: python fetch_dicts.py cs sk)
```

`fetch_dicts.py` downloads from a pinned commit of the LibreOffice repository, so every machine gets the same dictionaries; `--ref master` takes the newest instead. That is enough for spelling. For grammar, run a LanguageTool server and point `LT_URL` at it (default `http://localhost:8081/v2/check`):

- **Docker:** `docker run -d -p 8081:8010 erikvl87/languagetool:6.8`
- **Without Docker:** download the [LanguageTool desktop/server zip](https://languagetool.org/download/), then with Java 17+: `java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081`

The public LanguageTool API also works (`LT_URL=https://api.languagetool.org/v2/check`), but it has rate limits and sends your text to a third party. Don't use it for anything unpublished.

## Usage

```
python langcheck.py --lang cs "Pozvi kamaráda a získej odměnu"     # the copy itself, in quotes
python langcheck.py --lang pl copy.txt                            # plain text or Markdown, one check per paragraph
python langcheck.py page.html ads.json                            # several files; each can state its own language
python langcheck.py --lang de --all-strings strings.json          # every string value in any JSON
cat draft.md | python langcheck.py --lang en -                    # stdin
```

- HTML: visible text plus `alt`, `title`, `placeholder`, `aria-label`; language from `<html lang>`. Scripts, styles and code are skipped.
- JSON without `--all-strings`: the keys `headline(s)`, `title`, `subtitle`, `sub`, `subline`, `cta`, `text`, `copy`, `body`, `primary_text`, `description(s)`, `long_headline`, `subject`, `preheader`, at any depth; language from `lang`, `language` or `market`.
- `--ignore words.txt` (repeatable), plus `ignore.txt` next to the script: words that are right on purpose (your brand, product and feature names), one per line, case-insensitive.
- `--strict`: fail when any layer that applies could not run, not only when none did.
- `--json`: findings with tool, word, suggestions, rule, message, the text and the file it came from, plus which layers ran and which were skipped and why.
- `--no-languagetool` skips grammar; `--online` adds Korektor for Czech.
- `LANGTOOLS_DICTS` and `LT_URL` move the dictionaries and the server.

| Exit code | Meaning |
|---|---|
| 0 | clean |
| 1 | findings (always wins, even if a layer was missing) |
| 2 | bad input: no or unsupported language, file not found, not UTF-8, invalid JSON, a file with no copy in it |
| 3 | could not check: no layer ran for a language (or, with `--strict`, one of them did not) |

A missing layer never passes silently. Without dictionaries and without a server the result is exit 3 and `NOT CHECKED`, not "clean"; a mistyped path is exit 2, not a check of the path as if it were text.

Speed: spylls loads a dictionary in pure Python, which takes roughly 5 to 25 seconds per language (Czech is the slowest). It happens once per run, so pass all files of a job to one call.

## GitHub Action

Fail a pull request when the copy has a mistake:

```yaml
# .github/workflows/copy.yml
name: copy
on: [pull_request]
jobs:
  langcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: terezakohutova/copy-langcheck@v1   # this repository
        with:
          files: copy/*.json copy/sk-landing.html   # or one path per line
          lang: ''            # empty = each file says its own (HTML lang, JSON "lang")
          languagetool: true  # starts a pinned LanguageTool container on the runner
          strict: true        # fail if LanguageTool did not come up
          online: false       # true = also Korektor for Czech, text leaves the runner
          ignore: copy/brand-words.txt
```

The step exposes `exit-code` as an output. Dictionaries are cached between runs.

Two workflows test this repository on every push: [`tests.yml`](.github/workflows/tests.yml) runs the unit tests on Linux, macOS and Windows with Python 3.10 and 3.13, plus ruff; [`selftest.yml`](.github/workflows/selftest.yml) runs the action itself with a real LanguageTool container and checks exact exit codes: clean Slovak copy must give 0 in strict mode, the planted mistakes in `examples/` must give 1, and a missing file must give 2.

## Use it as a gate locally

```
python langcheck.py --lang cs ad-copy.json || { echo "fix the copy first"; exit 1; }
```

In an agent setup, make "langcheck exit 0" a required step before the agent may hand over or upload copy.

## Tests

```
python -m unittest -v
```

The unit tests run offline: a tiny fixture dictionary stands in for the LibreOffice ones and LanguageTool and Korektor are mocked, including their failure modes. If `./dicts` exists, the bundled examples are also checked against the real dictionaries.

## Korektor terms

Korektor's code is BSD-licensed, but the models behind the public LINDAT service are **CC BY-NC-SA**, and the service page says that commercial use needs explicit written permission of the authors and that submitted text may be kept to improve their systems. Checking commercial marketing copy through it is therefore off by default. Turn it on with `--online` (or `online: true` in the Action) only if your use is non-commercial or you have that permission. Czech is still checked by Hunspell without it.

## Limits, honestly

- **A wrong word that is also a real word passes.** Slovak `prehlad` instead of `prehľad` is in the dictionary, so Hunspell accepts it, and LanguageTool did not flag it in testing either. Read the copy yourself.
- **Korektor's suggestion can be wrong even when the flag is right.** In the Czech example it correctly flags `ziskej` but suggests `ziskem`; Hunspell suggests the right `získej`. Treat suggestions as hints.
- **No tone, truth or brand rules.** The tools don't know your tone of voice, your banned claims or whether a number is true.
- **Short headlines without a verb** can trip grammar rules. Formatting-only rules (missing full stop, typographic quotes) are already switched off.
- **Czech has no grammar layer here**, only spelling (Hunspell, plus Korektor if you may use it).

## Licence

The code is MIT, see [LICENSE](LICENSE). The dictionaries are downloaded from the LibreOffice repository by `fetch_dicts.py` and keep their own licences (MPL, LGPL, GPL and others, see the README in each folder there); they are not redistributed here. The two tiny dictionaries in `tests/fixtures/` are written for the tests. LanguageTool is LGPL. Korektor: see [Korektor terms](#korektor-terms).

---

### Česky

Kontrola pravopisu a gramatiky pro marketingové texty v češtině, slovenštině, polštině, němčině a angličtině. Spojuje Hunspell (lokálně, slovníky LibreOffice), LanguageTool (vlastní server, gramatika pro sk, pl, de, en) a volitelně Korektor z ÚFAL MFF UK (čeština, veřejné API; modely mají licenci CC BY-NC-SA, proto je ve výchozím stavu vypnutý). Nic nepřepisuje, jen hlásí: exit 1 při nálezu, exit 3 když se nic nemohlo zkontrolovat, takže může hlídat texty před nahráním reklamy nebo před vydáním webu. Instalace: `python -m pip install -r requirements.txt` a `python fetch_dicts.py`. Na tón, pravdivost a značku se dívá pořád člověk.
