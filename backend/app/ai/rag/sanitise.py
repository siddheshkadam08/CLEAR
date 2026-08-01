"""Neutralising document text before it reaches a prompt.

Every passage this platform puts in front of a model came out of a document
somebody else wrote. In contract review that somebody is frequently the
counterparty, and a PDF is a rich enough format to hide text from a human reader
while leaving it perfectly legible to a parser - white on white, zero-width
characters, a font size of 0.1pt, or an off-page text layer.

So retrieved text is treated as hostile input, not as content. Three specific
things are removed, each because it defeats a different part of the defence:

* **Delimiter forgery.** The answering prompt wraps evidence in
  ``<untrusted_evidence>`` tags and the grounding rules tell the model that
  everything inside them is data. A passage containing a closing tag ends the
  block early, and everything after it reads as though it came from the system.
  This is the one that turns a nuisance into an exploit.
* **Invisible characters.** Zero-width joiners, soft hyphens and bidi overrides
  carry no meaning in a contract and are exactly what hidden-instruction attacks
  use to break up words that would otherwise be filtered - or to make text that
  reads one way to a reviewer and another way to a model.
* **Control characters.** Never legitimate in extracted text, and they interfere
  with the delimiters and with log analysis downstream.

What is deliberately *not* done: no attempt to detect and strip "instructions".
That is unwinnable - the difference between a clause about notice and a sentence
telling the model what to say is semantic, and a filter aggressive enough to catch
the second removes real contract language. The defence is structural (delimiters
plus an explicit rule) rather than lexical, and this module only makes sure the
structure cannot be forged.
"""

from __future__ import annotations

import re
import unicodedata

#: Anything that could close, open or forge the evidence delimiter. Replaced
#: rather than deleted so a reader can still see something was there - a silently
#: vanished line is a worse artefact than a visible marker.
_DELIMITERS = re.compile(
    r"</?\s*(?:untrusted_evidence|system|instructions?|prompt)\s*>", re.IGNORECASE
)

#: Zero-width and directional formatting characters. None occur in legitimate
#: extracted contract text; all of them are standard tooling for hiding content.
_INVISIBLE = re.compile(
    "["
    "­"  # soft hyphen
    "​-‏"  # zero-width space/joiners, LRM/RLM
    "‪-‮"  # bidi embedding and override
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolates
    "﻿"  # BOM used mid-string
    "]"
)

#: C0/C1 controls except tab and newline, which carry layout.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]")

#: Marker left where a delimiter was. Visible in the answer's evidence and in the
#: audit record, so "why does this passage look odd" has an answer.
_REDACTED = "[removed: markup]"


def sanitise_evidence(text: str) -> str:
    """Make one passage safe to place inside a delimited prompt block.

    Idempotent, and never raises - a passage that cannot be cleaned is still
    better shown than dropped, because dropping it silently removes evidence the
    answer would otherwise have been graded against.
    """
    if not text:
        return ""

    # NFKC first: a delimiter written with fullwidth or mathematical look-alikes
    # normalises to its ASCII form here, so the pattern below sees it.
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _INVISIBLE.sub("", cleaned)
    cleaned = _CONTROL.sub(" ", cleaned)
    cleaned = _DELIMITERS.sub(_REDACTED, cleaned)
    return cleaned.strip()


def contains_suspicious_markup(text: str) -> bool:
    """True when a passage carried delimiter markup or hidden characters.

    Reported rather than acted on: a document that addresses an automated reader
    is a fact about the counterparty worth surfacing to a human, and it is not
    something to decide about silently inside a prompt builder.
    """
    if not text:
        return False
    normalised = unicodedata.normalize("NFKC", text)
    return bool(_DELIMITERS.search(normalised) or _INVISIBLE.search(text))


__all__ = ["contains_suspicious_markup", "sanitise_evidence"]
