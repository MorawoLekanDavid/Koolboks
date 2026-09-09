"""Picks which knowledge base documents are actually relevant to a given
message, instead of always concatenating every live document into every
single prompt. Scores by keyword overlap — no embeddings model is available
on the current provider (verified directly against the API, not assumed),
so this is the strongest retrieval achievable without adding a new paid
dependency. Isolated in its own module so a real embeddings-based version
can drop in later behind the same select_relevant_kb() signature without
touching any caller.
"""
import re

MAX_RELEVANT_DOCS = 4

_STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "does", "did", "i", "you", "we", "they",
    "it", "to", "for", "of", "in", "on", "at", "and", "or", "my", "your", "our",
    "this", "that", "have", "has", "want", "need", "can", "how", "what", "where",
    "when", "why", "which", "will", "would", "could", "should", "with", "about",
    "me", "us", "am", "be", "been", "im",
}


def _significant_words(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def select_relevant_kb(docs: list, query_text: str, max_docs: int = MAX_RELEVANT_DOCS) -> str:
    """`docs` is a list of {"name": str, "content": str}. Returns the joined text
    of whichever documents are relevant to `query_text`.

    Falls back to including everything when there are few enough documents that
    being selective wouldn't help, or when the query has no usable keyword
    signal (a bare "hi" or "ok") — better to hand the model slightly more
    context than to hand it none."""
    if not docs:
        return ""
    if len(docs) <= max_docs:
        return "\n\n---\n\n".join(d["content"] for d in docs)

    query_words = _significant_words(query_text)
    if not query_words:
        return "\n\n---\n\n".join(d["content"] for d in docs[:max_docs])

    scored = [(d, len(query_words & _significant_words(d["content"]))) for d in docs]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return "\n\n---\n\n".join(d["content"] for d, _score in scored[:max_docs])
