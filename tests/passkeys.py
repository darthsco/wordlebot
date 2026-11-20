"""A software authenticator for tests: answers the server's WebAuthn options with
really signed responses (EC P-256, "none" attestation), so the panel's checks run
for real without a browser."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

HOST = "panel.test"
ORIGIN = f"https://{HOST}"

UP, UV, AT = 0x01, 0x04, 0x40


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class Credential:
    id: bytes
    key: ec.EllipticCurvePrivateKey
    user_handle: bytes
    rp_id: str
    count: int = 0


class Authenticator:
    """One device. `counter=False` behaves like a synced passkey (sign count always 0)."""

    def __init__(self, counter: bool = True) -> None:
        self.counter = counter
        self.credentials: list[Credential] = []

    def _client_data(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict, *, origin: str = ORIGIN, rp_id: str | None = None, uv: bool = True) -> dict:
        rp_id = rp_id or options["rp"]["id"]
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        cred = Credential(secrets.token_bytes(16), key, unb64(options["user"]["id"]), rp_id)
        self.credentials.append(cred)
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([UP | AT | (UV if uv else 0)])
            + (0).to_bytes(4, "big")
            + bytes(16)  # AAGUID
            + len(cred.id).to_bytes(2, "big")
            + cred.id
            + cose
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64(cred.id),
            "rawId": b64(cred.id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(self._client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": b64(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict,
        *,
        origin: str = ORIGIN,
        uv: bool = True,
        credential: Credential | None = None,
        count: int | None = None,
    ) -> dict:
        allowed = {unb64(c["id"]) for c in options.get("allowCredentials") or []}
        if credential is None:
            credential = next(c for c in self.credentials if not allowed or c.id in allowed)
        if count is not None:
            credential.count = count
        elif self.counter:
            credential.count += 1
        auth_data = (
            hashlib.sha256(credential.rp_id.encode()).digest()
            + bytes([UP | (UV if uv else 0)])
            + credential.count.to_bytes(4, "big")
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = credential.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64(credential.id),
            "rawId": b64(credential.id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": b64(credential.user_handle),
            },
            "clientExtensionResults": {},
        }
