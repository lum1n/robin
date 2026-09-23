from robin.airlock import VocabularyTerm, fodselsnummer_ok, redact
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
    assert "[PERSON_1]" in first
    assert "[EMAIL_1]" in first
    assert "[PERSON_1]" in second
    assert vault.restore(first) == "Jane Doe <jane@example.com> called"


def test_another_conversation_does_not_restore_the_same_placeholder() -> None:
    ada = Vault("ada", "one")
    other = Vault("ada", "two")
    redact("Jane Doe", ada, vocabulary=(VocabularyTerm("Jane Doe"),))
    redact("Bob Berg", other, vocabulary=(VocabularyTerm("Bob Berg"),))
    assert ada.restore("[PERSON_1]") == "Jane Doe"
    assert other.restore("[PERSON_1]") == "Bob Berg"


def test_encrypted_vault_rejects_another_account() -> None:
    vault = Vault("ada", "thread")
    vault.token("PERSON", "Jane Doe")
    key = new_key()
    blob = vault.encrypt(key)
    opened = Vault.decrypt(blob, key, account_id="ada", conversation_id="thread")
    assert opened.restore("[PERSON_1]") == "Jane Doe"
    try:
        Vault.decrypt(blob, key, account_id="bea", conversation_id="thread")
    except VaultAccessError:
        return
    raise AssertionError("other account opened the vault")
