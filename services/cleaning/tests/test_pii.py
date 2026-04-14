"""Tests for PII removal."""

from core.pii import remove_pii


def test_email_removal():
    text = "Kontakt: max.mustermann@firma.at für Infos"
    result = remove_pii(text)
    assert "max.mustermann@firma.at" not in result
    assert "[EMAIL_REMOVED]" in result


def test_phone_plus43():
    assert "[PHONE_REMOVED]" in remove_pii("Tel: +43 1 234567")


def test_phone_0043():
    assert "[PHONE_REMOVED]" in remove_pii("Fax: 0043 664 1234567")


def test_phone_local():
    assert "[PHONE_REMOVED]" in remove_pii("Ruf: 0664 1234567")


def test_ip_removal():
    text = "Server: 192.168.1.100 ist erreichbar"
    result = remove_pii(text)
    assert "192.168.1.100" not in result
    assert "[IP_REMOVED]" in result


def test_no_pii_unchanged():
    text = "Hydraulikdruck zu hoch, Alarm 47"
    assert remove_pii(text) == text


def test_multiple_patterns():
    text = "Mail: a@b.at, Tel: +43 1 999, IP: 10.0.0.1"
    result = remove_pii(text)
    assert "[EMAIL_REMOVED]" in result
    assert "[PHONE_REMOVED]" in result
    assert "[IP_REMOVED]" in result
