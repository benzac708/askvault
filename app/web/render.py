"""Server-rendered HTML for `/`. One template file, no engine, no build step.

D27. The page is assembled with `string.Template` and `html.escape` from the
standard library: no Jinja, no Mako, no Node toolchain, nothing to compile
before the image can be built. For a page this size that is not a compromise,
it is the smaller thing.

Two properties this module exists to guarantee:

**Escaping.** Everything interpolated here is untrusted. The question is typed
by whoever is on the internet; the answer text and every citation field are
produced by a language model from a corpus that, in a real deployment, someone
else can edit. Interpolating any of them raw would be a stored-XSS hole with a
model in the middle of it, so every value goes through `html.escape` and there
is no code path that does not.

**The dollar rule.** `string.Template` scans the *template* for `$name`, so a
`$` anywhere in the template -- including inside the inline JavaScript, where a
template literal would be the natural thing to write -- is a substitution
attempt. `tests/test_web.py` asserts the set of placeholders matches exactly,
so a stray `$` is a test failure rather than a silently blank page.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from string import Template

from app.services.answer import Answer

TEMPLATE_PATH = Path(__file__).with_name("template.html")

# The complete set of substitutions the template is allowed to reference.
# Kept as data so the test can assert the template and this list agree, instead
# of both drifting the same way.
PLACEHOLDERS = frozenset(
    {
        "page_title",
        "query",
        "result_block",
        "index_block",
        "passages_total",
    }
)


@lru_cache(maxsize=1)
def _template() -> Template:
    # Read once. The file is baked into the image and never written at runtime,
    # and a container running readOnlyRootFilesystem could not rewrite it even
    # if something tried.
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _esc(value: object) -> str:
    """Escape a value for both element text and quoted attribute positions.

    `quote=True` matters: `query` lands inside a `value="..."` attribute, and
    without it a question containing a double quote would break out of the
    attribute rather than merely appearing in the text.
    """
    return html.escape(str(value), quote=True)


def _result_block(result: Answer | None, error: str) -> str:
    """Render the answer, its citations, or an error -- whichever applies."""
    if error:
        return (
            f'      <p class="kicker">Not answered</p>\n      <p class="notice">{_esc(error)}</p>'
        )

    if result is None:
        return (
            '      <p class="kicker none">No question asked</p>\n'
            '      <p class="pending">the answer, with its citations, appears here</p>'
        )

    grounded = result.passages_considered > 0
    count = result.passages_considered
    plural = "passage" if count == 1 else "passages"
    state = "result" if grounded else "result ungrounded"

    parts = [
        f'      <div class="{state}">',
        f'        <p class="prose">{_esc(result.answer)}</p>',
        "      </div>",
    ]

    if result.citations:
        parts.append('      <ol class="cites">')
        for i, citation in enumerate(result.citations, start=1):
            src = f'https://github.com/benzac708/askvault/blob/main/samples/{_esc(citation.doc)}.md'
            parts.append("        <li>")
            parts.append(f'          <a class="src" href="{src}" target="_blank" rel="noopener">')
            parts.append(f'            <span class="cnum">{i}</span>')
            parts.append(
                f'            <span class="cwhere">{_esc(citation.doc)}'
                f'<span class="sep">/</span>{_esc(citation.section)}</span>'
            )
            parts.append(f'            <p class="csnip">{_esc(citation.snippet)}</p>')
            parts.append("          </a>")
            parts.append("        </li>")
        parts.append("      </ol>")

    return "\n".join(parts)


def _index_block(index: Sequence[tuple[str, int]]) -> str:
    if not index:
        return '          <li><span class="doc">no index built</span><span>-</span></li>'
    rows = [
        f'          <li><span class="doc">{_esc(doc)}</span>'
        f"<span>{count} section{'' if count == 1 else 's'}</span></li>"
        for doc, count in index
    ]
    return "\n".join(rows)


def render_page(
    *,
    query: str = "",
    result: Answer | None = None,
    error: str = "",
    index: Sequence[tuple[str, int]] = (),
    total_passages: int = 0,
) -> str:
    """Assemble the full page.

    `index` is the list of documents actually present in the FTS index with
    their section counts, and `total_passages` the number of indexed sections.
    Both are read from the index rather than the corpus directory, so the page
    cannot advertise a document that failed to parse and cannot answer from.
    """
    title = f"{query} - AskVault" if query else "AskVault"
    return _template().safe_substitute(
        page_title=_esc(title),
        query=_esc(query),
        result_block=_result_block(result, error),
        index_block=_index_block(index),
        passages_total=_esc(
            f"{total_passages} section{'' if total_passages == 1 else 's'} indexed"
        ),
    )
