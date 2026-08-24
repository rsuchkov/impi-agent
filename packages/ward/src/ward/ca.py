"""A certificate authority small enough to reason about.

One CA, one server certificate for ward, one client certificate per agent and
one for the operator. That is the whole of it — no chains, no intermediates, no
revocation lists. Revocation is done by taking a name off a policy, which is
where authorization already lives.

The property being bought is **attribution**: ward learns which agent is asking
from the certificate the connection was made with, not from a header the caller
writes. It is worth being precise about what that does and does not mean.

*Does*: a process outside the agent's container cannot claim to be that agent,
and one agent cannot claim to be another.

*Does not*: distinguish ``secret-exec`` from anything else in the same
container. The key has to be readable by the agent's own processes for
``secret-exec`` to use it, so any other process there can read it too. No
scheme fixes that from inside; what limits the damage is that every request
still has to pass a human.

The CA key never leaves this container. Certificates are issued here, on
request, over the operator connection.
"""

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Ed25519 rather than RSA: smaller, faster, no key-size decision to get wrong,
# and supported by every TLS stack this runs against.
_CA_YEARS = 10
_LEAF_YEARS = 2

# The one name that is not an agent. A client certificate carrying it may drive
# the operator API; an agent's may only ask for a lease.
OPERATOR_CN = "operator"


@dataclass(frozen=True)
class Issued:
    """A certificate and the key that goes with it, PEM-encoded."""

    certificate: str
    key: str

    def write(self, cert_path: Path, key_path: Path) -> None:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_text(self.certificate, encoding="utf-8")
        key_path.write_text(self.key, encoding="utf-8")
        # The key is a credential: readable by its owner and nobody else.
        key_path.chmod(0o600)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _pem_key(key: ed25519.Ed25519PrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _pem_cert(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


class CertificateAuthority:
    """Creates the CA, and issues leaves against it."""

    def __init__(self, certificate: str, key: str) -> None:
        self._cert_pem = certificate
        self._cert = x509.load_pem_x509_certificate(certificate.encode())
        self._key = serialization.load_pem_private_key(key.encode(), password=None)

    @property
    def certificate(self) -> str:
        """The CA certificate, which every party needs in order to verify the
        others. Public: it authenticates nobody on its own."""
        return self._cert_pem

    @staticmethod
    def create(common_name: str = "ward-ca") -> tuple["CertificateAuthority", Issued]:
        key = ed25519.Ed25519PrivateKey.generate()
        now = _now()
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(_name(common_name))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))  # tolerate clock skew
            .not_valid_after(now + dt.timedelta(days=365 * _CA_YEARS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            # Spelled out because a path length without the signing bit is a
            # certificate OpenSSL refuses to build a chain through.
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            # OpenSSL will not build a chain without these: a leaf points at its
            # issuer by key id, and refuses to verify when it cannot.
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, None)
        )
        material = Issued(certificate=_pem_cert(cert), key=_pem_key(key))
        return CertificateAuthority(material.certificate, material.key), material

    @staticmethod
    def load(cert_path: Path, key_path: Path) -> "CertificateAuthority":
        return CertificateAuthority(
            cert_path.read_text(encoding="utf-8"), key_path.read_text(encoding="utf-8")
        )

    def issue_server(self, hostnames: tuple[str, ...]) -> Issued:
        """ward's own certificate. The names are how a client knows it reached
        ward and not something that answered instead."""
        alternatives: list[x509.GeneralName] = []
        for host in hostnames:
            try:
                alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                alternatives.append(x509.DNSName(host))
        return self._issue(
            hostnames[0],
            usage=ExtendedKeyUsageOID.SERVER_AUTH,
            alternatives=alternatives,
        )

    def issue_client(self, common_name: str) -> Issued:
        """A caller's certificate. The common name IS the identity ward will use
        — an agent's name, or ``operator``."""
        return self._issue(common_name, usage=ExtendedKeyUsageOID.CLIENT_AUTH)

    def _issue(
        self,
        common_name: str,
        *,
        usage: x509.ObjectIdentifier,
        alternatives: list[x509.GeneralName] | None = None,
    ) -> Issued:
        key = ed25519.Ed25519PrivateKey.generate()
        now = _now()
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=365 * _LEAF_YEARS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._cert.public_key()  # type: ignore[arg-type]
                ),
                critical=False,
            )
        )
        if alternatives:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(alternatives), critical=False
            )
        # None as the algorithm: Ed25519 signs the message itself, with no
        # separate digest to choose.
        cert = builder.sign(self._key, None)  # type: ignore[arg-type]
        return Issued(certificate=_pem_cert(cert), key=_pem_key(key))


def common_name_of(certificate_pem: str) -> str:
    """The identity a presented certificate claims. Called only after the TLS
    layer has verified the certificate against the CA — this reads a name, it
    does not establish one."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(names[0].value) if names else ""
