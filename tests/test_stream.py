"""LocalAgreement streaming: what gets typed, and that nothing is ever taken back."""

from utter.stream import StreamState, common_prefix, tokens


def test_tokens_keep_punctuation_attached():
    assert tokens("Wednesday, at  noon.") == ["Wednesday,", "at", "noon."]
    assert tokens("") == []


def test_common_prefix_ignores_case_and_trailing_punctuation():
    assert common_prefix(["wednesday", "at"], ["Wednesday,", "at"]) == 2
    assert common_prefix(["I", "think"], ["I", "thought"]) == 1


def test_nothing_is_typed_until_two_passes_agree():
    s = StreamState(agree=2, lag=0)
    assert s.offer("hello world") == ""
    assert s.offer("hello world") == "Hello world"


def test_lag_holds_back_the_newest_words():
    s = StreamState(agree=2, lag=2)
    s.offer("hello there my friend")
    assert s.offer("hello there my friend how") == "Hello there"
    assert s.offer("hello there my friend how are") == " my"
    assert s.typed == "Hello there my"


def test_disagreement_commits_only_the_shared_prefix():
    s = StreamState(agree=2, lag=0)
    s.offer("I think we should")
    assert s.offer("I thought we should") == "I"


def test_typed_words_are_never_revised():
    s = StreamState(agree=1, lag=0)
    assert s.offer("hello world") == "Hello world"
    # The final pass disagrees about the start. We only append what is missing.
    assert s.finish("goodbye world and more") == " and more"
    assert s.typed == "Hello world and more"


def test_finish_adds_the_unsaid_tail():
    s = StreamState(agree=2, lag=2)
    s.offer("ask not what your")
    s.offer("ask not what your country")
    assert s.typed == "Ask not"
    assert s.finish("ask not what your country can do") == " what your country can do"


def test_finish_drops_a_repeated_trailing_clause():
    s = StreamState(agree=1, lag=0)
    s.offer("ask what you can do")
    assert s.finish("ask what you can do ask what you can do") == ""


def test_sentence_case_carries_across_chunks():
    s = StreamState(agree=1, lag=0)
    assert s.offer("it works.") == "It works."
    assert s.offer("it works. then more") == " Then more"


def test_reset_starts_a_fresh_utterance():
    s = StreamState(agree=1, lag=0)
    s.offer("one two")
    s.reset()
    assert s.typed == ""
    assert s.offer("three") == "Three"
