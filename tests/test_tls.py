import hashlib
import ssl

from app.tls import max_verify


def test_max_trusts_pinned_certificate_and_still_checks_hostname():
    context=max_verify('https://platform-api2.max.ru')
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    fingerprints={hashlib.sha256(c).hexdigest() for c in context.get_ca_certs(binary_form=True)}
    assert 'd26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31' in fingerprints


def test_extra_ca_does_not_apply_to_sandbox_or_unrelated_hosts():
    for url in ('http://localhost:8031','https://example.org','https://platform-api2.max.ru.example.org'):
        assert max_verify(url) is True
