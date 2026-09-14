"""Ed25519 signature verification, in the standard library alone.

This module exists because of a specific piece of dishonesty it replaces. The
release trust anchor used to carry a ``signature`` field that nothing checked,
with a comment explaining that the standard library has no signature
primitive. That is true of the standard library's *API*, and false about what
the standard library can compute: Ed25519 verification is modular arithmetic
over a fixed curve plus SHA-512, and Python has both.

So the choice was never "verify or admit we cannot". It was "verify, or carry
a decorative field". A decorative signature field is worse than no field,
because it reads as protection to everyone who does not open this file.

What is implemented is verification only, from RFC 8032 section 6. There is
no signing here and there will not be: a release is signed on the machine
that holds the release key, which is not this one, and code that could sign
is code an attacker who reaches this process could sign with.

Why this is not delegated to a vetted library
---------------------------------------------

The obvious answer to "do not write your own crypto" is to call one that was
audited. That answer was tried and measured rather than assumed, and it does
not hold here for two reasons.

The first is availability. This project is standard library only on Python
3.11+, and the guest image and a stock Windows Python both ship without
``cryptography`` or any other Ed25519 implementation. A trust check that is
skipped when an optional import is missing is a trust check an attacker
removes by uninstalling a package.

The second is that it would not have fixed the defect that prompted this
rewrite. The first version of this file accepted the small-order forgery
``A = R = <identity>, S = 0``, which verifies against *any* message. OpenSSL,
through ``cryptography`` 50.0.0, accepts that same forgery, and the order-2
variant as well; both were run against it. A "vetted verifier" would have
inherited the bug, not removed it. What removes it is rejecting small-order
public keys and commitments, which is done below and is strictly stronger
than the library behaviour measured.

What this is checked against, exactly
-------------------------------------

* The RFC 8032 section 7.1 test vectors, all four, message and signature
  byte for byte.
* A round trip against a test-only signer, so a genuine signature verifies.
* The eight small-order point encodings, each rejected as a public key and
  as a commitment.
* Non-canonical ``y`` (``y >= p``) and non-canonical ``S`` (``S >= L``).

That is the claim, and it is the whole claim. This is not an audited
implementation, it is not constant-time, and passing those vectors is not a
proof of correctness for every input. It is stated here in full so nobody has
to infer the coverage from the fact that the file mentions an RFC.

Verification is not constant-time and does not need to be. Every input is
public: the manifest bytes, the release public key, and the signature that
shipped beside them. There is no secret in this file for a timing channel to
leak.
"""

from __future__ import annotations

import hashlib

#: The prime field and group order of edwards25519. Fixed by RFC 8032; these
#: are not tunable parameters and nothing here should ever read them from
#: outside.
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493

_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

#: A decompressed point is ``(X, Y, Z, T)`` in extended coordinates, which is
#: what keeps the scalar multiplication loop free of modular inversions. Only
#: decompression and the final comparison touch ``Z``.
_IDENTITY = (0, 1, 1, 0)


class SignatureError(ValueError):
    """The signature, key or message was not the right shape to check."""


def _modp_inv(value: int) -> int:
    return pow(value, _P - 2, _P)


def _recover_x(y: int, sign: int) -> int | None:
    """The x matching this y on the curve, or None if there is not one."""

    if y >= _P:
        return None
    x2 = (y * y - 1) * _modp_inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        # y was not the ordinate of any curve point. A forged or corrupt key.
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_G_Y = 4 * _modp_inv(5) % _P
_G_X = _recover_x(_G_Y, 0)
assert _G_X is not None  # the fixed base point is on the fixed curve
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _point_add(first: tuple[int, int, int, int],
               second: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (first[1] - first[0]) * (second[1] - second[0]) % _P
    b = (first[1] + first[0]) * (second[1] + second[0]) % _P
    c = 2 * first[3] * second[3] * _D % _P
    e = 2 * first[2] * second[2] % _P
    f, g, h, i = b - a, e - c, e + c, b + a
    return (f * g % _P, h * i % _P, g * h % _P, f * i % _P)


def _point_mul(scalar: int, point: tuple[int, int, int, int]
               ) -> tuple[int, int, int, int]:
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(first: tuple[int, int, int, int],
                 second: tuple[int, int, int, int]) -> bool:
    if (first[0] * second[2] - second[0] * first[2]) % _P != 0:
        return False
    return (first[1] * second[2] - second[1] * first[2]) % _P == 0


def _decompress(payload: bytes) -> tuple[int, int, int, int] | None:
    if len(payload) != 32:
        return None
    y = int.from_bytes(payload, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _sha512_modq(payload: bytes) -> int:
    return int.from_bytes(hashlib.sha512(payload).digest(), "little") % _Q


#: The edwards25519 cofactor. A point ``P`` lies in the small-order torsion
#: subgroup exactly when ``[8]P`` is the identity, so this is the whole test
#: and there is no hard-coded table of encodings to get wrong.
_COFACTOR = 8


def _is_small_order(point: tuple[int, int, int, int]) -> bool:
    """True for the eight points an attacker can use without a private key.

    This is the check whose absence made the first version of this file
    forgeable. With ``A`` the identity, the verification equation collapses to
    ``[S]B == R``, which ``R = A, S = 0`` satisfies for every message: the
    message never enters the arithmetic, so one 64-byte constant "signs"
    anything. The order-2 point does the same. Neither requires knowing any
    private key.

    Rejecting the whole torsion subgroup rather than the two exploitable
    points is deliberate. The exploitable set depends on the shape of the
    equation, and the equation is easier to change than this file is to
    re-audit.
    """

    return _point_equal(_point_mul(_COFACTOR, point), _IDENTITY)


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """True only for a signature this key actually produced over this message.

    Every rejection is a plain ``False``. There is deliberately no vocabulary
    distinguishing "malformed key" from "wrong signature": a caller that
    branched on the difference would be building an oracle out of a check
    whose only useful answer is yes.
    """

    if len(public_key) != 32 or len(signature) != 64:
        return False
    key_point = _decompress(public_key)
    if key_point is None:
        return False
    if _is_small_order(key_point):
        # A key with no private half. See _is_small_order.
        return False
    commitment = signature[:32]
    commitment_point = _decompress(commitment)
    if commitment_point is None:
        return False
    if _is_small_order(commitment_point):
        return False
    scalar = int.from_bytes(signature[32:], "little")
    if scalar >= _Q:
        # Non-canonical S. Accepting it is how implementations acquire
        # malleable signatures, where two distinct signatures verify.
        return False
    challenge = _sha512_modq(commitment + public_key + message)
    return _point_equal(_point_mul(scalar, _G),
                        _point_add(commitment_point,
                                   _point_mul(challenge, key_point)))


def verify_hex(message: bytes, signature_hex: str, public_key_hex: str) -> bool:
    """The same check, taking the hex forms a manifest anchor carries.

    Anything that is not 64 hex characters of key and 128 of signature is
    rejected here rather than being padded, truncated or guessed at.
    """

    try:
        signature = bytes.fromhex(signature_hex.strip())
        public_key = bytes.fromhex(public_key_hex.strip())
    except (ValueError, AttributeError):
        return False
    return verify(message, signature, public_key)


#: Exactly what this module proves, in the words used where it is relied on.
SIGNATURE_GUARANTEE = (
    "an Ed25519 signature over the canonical manifest bytes, checked against "
    "the public key the release anchor names, with small-order keys and "
    "commitments and non-canonical S rejected. It proves the holder of that "
    "release's private key signed these exact bytes. It proves nothing about "
    "whether that key is the right one to trust, which is what pinning the "
    "key in the shipped anchor is for"
)

#: The coverage this implementation actually has, stated so that no reader has
#: to infer it from the presence of an RFC number.
VERIFICATION_COVERAGE = (
    "Checked against the four RFC 8032 section 7.1 vectors, a genuine "
    "round trip, the eight small-order encodings rejected in both the key and "
    "the commitment position, non-canonical y and non-canonical S. Not an "
    "audited implementation and not constant-time; every input it sees is "
    "public"
)
