"""Deciding which words are safe to type while you are still speaking.

Re-recognising a growing audio buffer gives a different guess each pass. "I think we"
becomes "I thought we" a second later. If we typed every guess we would be constantly
deleting and retyping text inside someone else's application, which looks broken and
fights autocomplete.

So we only ever type words we believe are settled, and never take anything back. A word
counts as settled when consecutive passes agree on it -- the local-agreement idea from
streaming-ASR work: hold a hypothesis until it stops changing, then commit its stable
prefix and never revisit it.

The result is append-only output: text appears a beat behind your voice, and does not
rewrite itself.
"""

from __future__ import annotations

import re

# Split into words while keeping punctuation attached, so "Wednesday," is one token.
_WORD = re.compile(r"\S+")


def tokens(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _norm(token: str) -> str:
    """Compare words ignoring case and trailing punctuation.

    A recogniser will happily change "wednesday" to "Wednesday," once it sees the rest
    of the sentence. That is not a different word, and waiting for it to stop wobbling
    would stall the stream.
    """
    return token.lower().strip(".,!?;:\"'()[]-")


def common_prefix(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if _norm(x) != _norm(y):
            break
        n += 1
    return n


def _capitalise(chunk: str, capitalise_next: bool) -> tuple[str, bool]:
    """Fix sentence case in text we are about to type.

    Append-only streaming commits early words before the recogniser has seen the rest
    of the sentence, so the first word often goes out lowercase and can never be taken
    back. Capitalising as we emit costs nothing and removes the most visible artefact.

    Returns the text plus whether the NEXT chunk should start a sentence -- a chunk can
    end on a full stop, and the following chunk is typed separately.
    """
    if not chunk:
        return chunk, capitalise_next
    out = list(chunk)
    for i, ch in enumerate(out):
        if capitalise_next and ch.isalpha():
            out[i] = ch.upper()
            capitalise_next = False
        elif ch in ".!?":
            capitalise_next = True
    return "".join(out), capitalise_next


def _trim_repeat(committed: list[str], tail: list[str]) -> list[str]:
    """Drop a tail that merely repeats what was said just before it.

    Whisper loops on trailing silence, so the final pass often ends by restating the
    last clause. The repeat straddles the boundary between text already typed and the
    tail, so it has to be caught here rather than in sentence-level cleanup -- and only
    the tail can be trimmed, since the committed part is already in the user's window.
    """
    if not tail:
        return tail
    combined = committed + tail
    longest = min(len(tail), len(combined) // 2)
    for n in range(longest, 2, -1):
        a = [_norm(w) for w in combined[-n:]]
        b = [_norm(w) for w in combined[-2 * n : -n]]
        if a == b:
            return tail[: len(tail) - n]
    return tail


class StreamState:
    """Tracks what has already been typed and what is safe to type next.

    Usage per partial transcript:

        chunk = state.offer(partial_text)   # text to type now, may be ""
        ...
        tail = state.finish(final_text)     # everything still unsaid
    """

    def __init__(self, agree: int = 2, lag: int = 1) -> None:
        # How many consecutive passes must agree before a word is committed.
        self.agree = max(1, agree)
        # Hold back this many settled words anyway. The last word of a partial is the
        # one most likely to change once the next syllable arrives, so keeping one in
        # hand costs a little latency and avoids most corrections.
        self.lag = max(0, lag)
        self.committed: list[str] = []
        self._history: list[list[str]] = []
        self._emitted = False
        self._cap_next = True

    @property
    def typed(self) -> str:
        return " ".join(self.committed)

    def offer(self, partial: str) -> str:
        """Feed a new partial transcript. Returns the text to append now."""
        words = tokens(partial)
        if not words:
            return ""
        self._history.append(words)
        if len(self._history) > self.agree:
            self._history.pop(0)
        if len(self._history) < self.agree:
            return ""

        # The prefix every recent pass agrees on.
        stable = self._history[0]
        for other in self._history[1:]:
            stable = stable[: common_prefix(stable, other)]

        # Keep a word or two in hand; they are the ones still likely to change.
        safe = stable[: max(0, len(stable) - self.lag)]
        if len(safe) <= len(self.committed):
            return ""

        # Trust the newest spelling of the words we are about to emit: later passes
        # punctuate and capitalise better than earlier ones.
        newest = self._history[-1]
        fresh = newest[len(self.committed) : len(safe)]
        if not fresh:
            return ""
        chunk, self._cap_next = _capitalise(" ".join(fresh), self._cap_next)
        prefix = " " if self._emitted else ""
        self.committed.extend(fresh)
        self._emitted = True
        return prefix + chunk

    def finish(self, final: str) -> str:
        """Return whatever the final transcript adds beyond what was already typed."""
        words = tokens(final)
        if not words:
            return ""
        keep = min(len(self.committed), common_prefix(self.committed, words))
        # If the final text diverges from what we typed, we do not rewrite history --
        # we only add what is missing from the point the two still agreed.
        tail = words[keep:] if keep >= len(self.committed) else words[len(self.committed) :]
        tail = _trim_repeat(self.committed, tail)
        if not tail:
            return ""
        text, self._cap_next = _capitalise(" ".join(tail), self._cap_next)
        prefix = " " if self._emitted else ""
        self.committed.extend(tail)
        self._emitted = True
        return prefix + text

    def reset(self) -> None:
        self.committed.clear()
        self._history.clear()
        self._emitted = False
        self._cap_next = True
