"""SessionProofBook: the per-session secret behind a believable session id."""

from crucible.tools import SessionProofBook


def test_a_proof_vouches_for_its_session_only_while_issued() -> None:
    book = SessionProofBook()
    proof = book.issue("assistant--c1")
    assert book.verify("assistant--c1", proof) is True
    assert book.verify("assistant--c2", proof) is False  # another conversation
    assert book.verify("assistant--c1", "guess") is False
    assert book.verify("assistant--c1", "") is False
    assert book.verify("assistant--never", "") is False  # never issued: never proven

    renewed = book.issue("assistant--c1")  # a respawn: the old copy is dead
    assert renewed != proof
    assert book.verify("assistant--c1", proof) is False
    assert book.verify("assistant--c1", renewed) is True

    book.revoke("assistant--c1")
    assert book.verify("assistant--c1", renewed) is False
