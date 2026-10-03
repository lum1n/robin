from robin.airlock import UNRESOLVED, VocabularyTerm, fodselsnummer_ok, redact, release
from robin.vault import Vault, VaultAccessError, new_key

FODSELSNUMMER = "01010000110"
KONTONUMMER = "12345678903"
ORGANISASJONSNUMMER = "123456785"
CARD = "4242424242424242"
IBAN = "NO9386011117947"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_checksums_match_hand_computed_vectors() -> None:
    assert fodselsnummer_ok(FODSELSNUMMER)
    assert not fodselsnummer_ok("01010000111")


def test_critical_values_are_dropped_and_not_restored() -> None:
    vault = Vault("ada", "thread")
    text = f"id {FODSELSNUMMER} key {SECRET} card {CARD} iban {IBAN} konto {KONTONUMMER} org {ORGANISASJONSNUMMER}"
    redacted, report = redact(text, vault)
    for secret in (FODSELSNUMMER, SECRET, CARD, IBAN, KONTONUMMER, ORGANISASJONSNUMMER):
        assert secret not in redacted
        assert not vault.contains_value(secret)
    assert report.has_critical
    assert vault.restore(redacted) == redacted


def test_tokenize_round_trip_is_stable_inside_one_conversation() -> None:
    vault = Vault("ada", "thread")
    vocabulary = (VocabularyTerm("Jane Doe", "PERSON"),)
    first, _ = redact("Jane Doe <jane@example.com> called", vault, vocabulary=vocabulary)
    second, _ = redact("Jane Doe wrote again", vault, vocabulary=vocabulary)
    assert "Jane Doe" not in first
    assert "jane@example.com" not in first
    assert vault.token("PERSON", "Jane Doe") in first
    assert vault.token("EMAIL", "jane@example.com") in first
    assert vault.token("PERSON", "Jane Doe") in second
    assert vault.restore(first) == "Jane Doe <jane@example.com> called"


def test_another_conversation_does_not_restore_the_same_placeholder() -> None:
    ada = Vault("ada", "one")
    other = Vault("ada", "two")
    redact("Jane Doe", ada, vocabulary=(VocabularyTerm("Jane Doe"),))
    redact("Bob Berg", other, vocabulary=(VocabularyTerm("Bob Berg"),))
    jane = ada.token("PERSON", "Jane Doe")
    bob = other.token("PERSON", "Bob Berg")
    assert jane != bob
    assert ada.restore(jane) == "Jane Doe"
    assert other.restore(jane) == jane
    assert other.restore(bob) == "Bob Berg"


def test_encrypted_vault_rejects_another_account() -> None:
    vault = Vault("ada", "thread")
    reference = vault.token("PERSON", "Jane Doe")
    key = new_key()
    blob = vault.encrypt(key)
    opened = Vault.decrypt(blob, key, account_id="ada", conversation_id="thread")
    assert opened.restore(reference) == "Jane Doe"
    try:
        Vault.decrypt(blob, key, account_id="bea", conversation_id="thread")
    except VaultAccessError:
        return
    raise AssertionError("other account opened the vault")


def test_release_keeps_usable_text_when_ner_is_unavailable() -> None:
    vault = Vault("ada", "thread")
    vocabulary = (VocabularyTerm("Jane Doe", "PERSON"),)
    text = "Jane Doe wrote jane@example.com about soccer practice. Next bus is at 08:10."
    out = release(text, vault, vocabulary=vocabulary, free_text=True, ner_available=False)
    assert out != UNRESOLVED
    assert "Jane Doe" not in out
    assert "jane@example.com" not in out
    assert "soccer practice" in out
    assert "08:10" in out
    assert vault.token("PERSON", "Jane Doe") in out
    assert vault.token("EMAIL", "jane@example.com") in out


def test_release_still_drops_secrets_when_ner_is_unavailable() -> None:
    vault = Vault("ada", "thread")
    text = f"key {SECRET} id {FODSELSNUMMER} card {CARD} then buy milk"
    out = release(text, vault, free_text=True, ner_available=False)
    assert SECRET not in out
    assert FODSELSNUMMER not in out
    assert CARD not in out
    assert "[REDACTED]" in out
    assert "buy milk" in out
    assert out != UNRESOLVED
