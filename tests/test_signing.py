"""Ed25519 verification, against the standard's own vectors.

This module replaced a ``signature`` field that nothing checked. The tests
that matter most are therefore the negative ones: a signature that does not
verify, a key that did not sign, and a malformed input must all be plain
``False`` rather than an exception the caller might treat as "unknown".
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import signing


# RFC 8032 section 7.1, test vectors 1 to 3, as (public key, message,
# signature) in hex. Present so this implementation is checked against the
# standard rather than against itself.
RFC_8032_VECTORS = (
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
     "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
)


def sign(message: bytes, seed: bytes) -> tuple[bytes, bytes]:
    """A test-only Ed25519 signer, RFC 8032 section 5.1.6.

    Signing lives here and not in the production module on purpose. A release
    is signed on the machine holding the release key; code in the shipped
    package that could sign is code an attacker reaching that process could
    sign with. Tests need a signature over arbitrary bytes, so they make one.
    """

    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    prefix = digest[32:]
    public = _compress(signing._point_mul(scalar, signing._G))
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(),
                           "little") % signing._Q
    commitment = _compress(signing._point_mul(nonce, signing._G))
    challenge = signing._sha512_modq(commitment + public + message)
    value = (nonce + challenge * scalar) % signing._Q
    return public, commitment + value.to_bytes(32, "little")


def _compress(point) -> bytes:
    x, y, z, _ = point
    inverse = signing._modp_inv(z)
    x, y = x * inverse % signing._P, y * inverse % signing._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


class StandardVectorTests(unittest.TestCase):
    def test_every_rfc_8032_vector_verifies(self):
        for public, message, signature in RFC_8032_VECTORS:
            with self.subTest(public=public[:8]):
                self.assertTrue(signing.verify_hex(bytes.fromhex(message),
                                                   signature, public))

    def test_a_changed_message_does_not_verify(self):
        public, message, signature = RFC_8032_VECTORS[1]
        self.assertFalse(signing.verify_hex(bytes.fromhex(message) + b"!",
                                            signature, public))

    def test_a_different_key_does_not_verify(self):
        _public, message, signature = RFC_8032_VECTORS[1]
        other = RFC_8032_VECTORS[0][0]
        self.assertFalse(signing.verify_hex(bytes.fromhex(message), signature,
                                            other))

    def test_a_flipped_signature_bit_does_not_verify(self):
        public, message, signature = RFC_8032_VECTORS[2]
        raw = bytearray(bytes.fromhex(signature))
        raw[0] ^= 0x01
        self.assertFalse(signing.verify(bytes.fromhex(message), bytes(raw),
                                        bytes.fromhex(public)))


class MalformedInputTests(unittest.TestCase):
    """Rejection, never an exception. A raised error is not a verdict."""

    def test_wrong_lengths_are_false(self):
        for signature, key in ((b"", b"\x00" * 32), (b"\x00" * 64, b""),
                               (b"\x00" * 63, b"\x00" * 32),
                               (b"\x00" * 64, b"\x00" * 31)):
            self.assertFalse(signing.verify(b"x", signature, key))

    def test_a_key_that_is_not_a_curve_point_is_false(self):
        self.assertFalse(signing.verify(b"x", b"\x00" * 64, b"\xff" * 32))

    def test_non_hex_input_is_false_rather_than_raising(self):
        for signature, key in (("zz", "00" * 32), ("00" * 64, "zz"),
                               ("", ""), (None, None)):
            self.assertFalse(signing.verify_hex(b"x", signature, key))

    def test_a_non_canonical_scalar_is_refused(self):
        """Accepting S >= L is how an implementation becomes malleable."""
        public, message, signature = RFC_8032_VECTORS[1]
        raw = bytearray(bytes.fromhex(signature))
        scalar = int.from_bytes(raw[32:], "little") + signing._Q
        if scalar < 1 << 256:
            raw[32:] = scalar.to_bytes(32, "little")
            self.assertFalse(signing.verify(bytes.fromhex(message), bytes(raw),
                                            bytes.fromhex(public)))


class RoundTripTests(unittest.TestCase):
    """The test-only signer and the shipped verifier agree."""

    def test_a_signature_over_arbitrary_bytes_verifies(self):
        message = b"a manifest, canonically serialised"
        public, signature = sign(message, b"s" * 32)
        self.assertTrue(signing.verify(message, signature, public))

    def test_a_signature_from_one_key_fails_under_another(self):
        message = b"a manifest"
        public, signature = sign(message, b"s" * 32)
        other, _ = sign(message, b"t" * 32)
        self.assertNotEqual(public, other)
        self.assertFalse(signing.verify(message, signature, other))


if __name__ == "__main__":
    unittest.main()


#: The eight encodings of points whose order divides the cofactor, from the
#: published "Taming the many EdDSAs" test set. Written out as literals rather
#: than computed, so this test disagrees with the module if the module's own
#: derivation of the set is wrong.
SMALL_ORDER_ENCODINGS = (
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0000000000000000000000000000000000000000000000000000000000000080",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
)

#: The exact forgery an external reviewer found this module accepting: the
#: identity point as both the public key and the commitment, with a zero
#: scalar. It verifies against any message under a verifier that does not
#: reject small-order points, including OpenSSL's.
IDENTITY_KEY = bytes([1] + [0] * 31)
ZERO_SCALAR = (0).to_bytes(32, "little")


class SmallOrderForgeryTests(unittest.TestCase):
    """The forgery that needs no private key, and the subgroup it lives in.

    This is a regression in the strict sense: the first version of this module
    returned True for every one of these.
    """

    def test_the_identity_key_forgery_is_refused(self):
        self.assertFalse(signing.verify(b"attacker chosen message",
                                        IDENTITY_KEY + ZERO_SCALAR,
                                        IDENTITY_KEY))

    def test_the_identity_key_forgery_is_refused_for_every_message(self):
        """The whole point of the forgery is that the message never enters the
        arithmetic. If any message verified, all of them would."""

        for message in (b"", b"a", b"release manifest", bytes(range(256))):
            self.assertFalse(signing.verify(message,
                                            IDENTITY_KEY + ZERO_SCALAR,
                                            IDENTITY_KEY), message[:16])

    def test_the_order_two_forgery_is_refused(self):
        point = bytes.fromhex(SMALL_ORDER_ENCODINGS[1])
        self.assertFalse(signing.verify(b"anything", point + ZERO_SCALAR, point))

    def test_no_small_order_point_is_accepted_as_a_public_key(self):
        for encoding in SMALL_ORDER_ENCODINGS:
            point = bytes.fromhex(encoding)
            for scalar in (ZERO_SCALAR, (1).to_bytes(32, "little")):
                self.assertFalse(signing.verify(b"m", point + scalar, point),
                                 encoding)

    def test_no_small_order_point_is_accepted_as_a_commitment(self):
        """Even under a real key, a torsion commitment is refused outright
        rather than left to fail the equation by luck."""

        public, _signature = sign(b"m", b"\x07" * 32)
        for encoding in SMALL_ORDER_ENCODINGS:
            commitment = bytes.fromhex(encoding)
            self.assertFalse(
                signing.verify(b"m", commitment + ZERO_SCALAR, public), encoding)

    def test_the_module_derives_the_same_eight_points(self):
        """The module computes the subgroup rather than listing it. This is
        the independent check that its computation agrees with the published
        set, in both directions."""

        derived = []
        for encoding in SMALL_ORDER_ENCODINGS:
            point = signing._decompress(bytes.fromhex(encoding))
            self.assertIsNotNone(point, encoding)
            derived.append(signing._is_small_order(point))
        self.assertEqual(derived, [True] * len(SMALL_ORDER_ENCODINGS))

    def test_a_real_public_key_is_not_small_order(self):
        """The rejection must not be so broad that it refuses genuine keys."""

        for seed in (b"\x01" * 32, b"\x02" * 32, b"\xfe" * 32):
            public, _signature = sign(b"m", seed)
            point = signing._decompress(public)
            self.assertIsNotNone(point)
            self.assertFalse(signing._is_small_order(point))

    def test_genuine_signatures_still_verify_after_the_hardening(self):
        message = b"canonical manifest bytes"
        public, signature = sign(message, b"\x11" * 32)
        self.assertTrue(signing.verify(message, signature, public))


class CoverageHonestyTests(unittest.TestCase):
    """The module states its coverage rather than implying an audit."""

    def test_the_coverage_note_does_not_claim_more_than_is_tested(self):
        note = signing.VERIFICATION_COVERAGE
        self.assertIn("Not an", note)
        self.assertIn("audited", note)
        self.assertIn("small-order", note)

    def test_the_guarantee_mentions_the_small_order_rejection(self):
        self.assertIn("small-order", signing.SIGNATURE_GUARANTEE)
