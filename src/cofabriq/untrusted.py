"""Untrusted text in prompts and in the checks after them.

Issues, documents, check output and agents' findings reach a model as data
in a tag (`fenced`); a quote the model gives back is accepted only if it is
in the text it saw (`quote_in`).
"""


def fenced(tag: str, text: str, **attrs: str) -> str:
    """`text` in a tag it can't close early."""
    head = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f"<{tag}{head}>\n{text.replace(f'</{tag}', f'</ {tag}')}\n</{tag}>"


def quote_in(quote: str, text: str, *, fold_case: bool = False) -> bool:
    """`quote` is in `text`, whitespace runs read as one space, so a quote
    across a line break still matches. `fold_case`: capitalisation too."""
    q, t = " ".join(quote.split()), " ".join(text.split())
    if fold_case:
        q, t = q.casefold(), t.casefold()
    return q in t
