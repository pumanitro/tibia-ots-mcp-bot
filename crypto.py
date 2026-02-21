"""
OT Protocol Cryptography - XTEA and RSA for Open Tibia protocol.
"""

import struct
from Crypto.PublicKey import RSA


# Default OTClient RSA key (used by many OTS servers)
# This is the well-known public key from the OTClient source
DEFAULT_RSA_N = int(
    "109120132967399429278860960508995541528237502902798129123468757937266291492576446330739696001110603907230888610072655818825358503429057592827629436413108566029093628212635953836686562675849720620786279431090218017681061521755056710823876476444260558147179707119674283982419152118103759076030616683978566631413"
)
DEFAULT_RSA_E = 65537
DEFAULT_RSA_D = int(
    "46730330223584118622160180015036832148732986808519344675210555262940258739805766860224610646919605860206328024326703361630109888417839241959507572247284807035235569619173792292786907845791904955103601652822519121908367187885509270025388641700821735345222087940578381210879116823013776808975766851829020659073"
)


def generate_proxy_rsa_keypair():
    """Generate a new 1024-bit RSA key pair for the proxy."""
    key = RSA.generate(1024)
    return key


def get_default_rsa_key():
    """Get the default OTClient RSA key."""
    return RSA.construct((DEFAULT_RSA_N, DEFAULT_RSA_E, DEFAULT_RSA_D))


def rsa_decrypt(data: bytes, private_key: RSA.RsaKey) -> bytes:
    """
    Decrypt RSA block (raw/textbook RSA, no padding scheme).
    OTClient uses raw RSA (modular exponentiation), NOT PKCS#1.
    """
    # Convert bytes to integer
    encrypted_int = int.from_bytes(data, byteorder='big')
    # Raw RSA decryption: m = c^d mod n
    decrypted_int = pow(encrypted_int, private_key.d, private_key.n)
    # Convert back to bytes (128 bytes for 1024-bit key)
    key_size = (private_key.n.bit_length() + 7) // 8
    return decrypted_int.to_bytes(key_size, byteorder='big')


def rsa_encrypt(data: bytes, public_key: RSA.RsaKey) -> bytes:
    """
    Encrypt with RSA (raw/textbook RSA, no padding scheme).
    OTClient uses raw RSA (modular exponentiation), NOT PKCS#1.
    """
    # Convert bytes to integer
    plain_int = int.from_bytes(data, byteorder='big')
    # Raw RSA encryption: c = m^e mod n
    encrypted_int = pow(plain_int, public_key.e, public_key.n)
    # Convert back to bytes
    key_size = (public_key.n.bit_length() + 7) // 8
    return encrypted_int.to_bytes(key_size, byteorder='big')


def xtea_decrypt(data: bytes, key: tuple[int, int, int, int]) -> bytes:
    """
    Decrypt data using XTEA algorithm (as used in OT protocol).

    Args:
        data: Encrypted data (must be multiple of 8 bytes)
        key: Tuple of 4 uint32 values

    Returns:
        Decrypted data
    """
    if len(data) % 8 != 0:
        raise ValueError(f"Data length ({len(data)}) must be multiple of 8")

    result = bytearray()
    num_rounds = 32
    delta = 0x61C88647

    for i in range(0, len(data), 8):
        v0, v1 = struct.unpack_from('<II', data, i)
        sum_val = 0xC6EF3720  # (delta * num_rounds) & 0xFFFFFFFF

        for _ in range(num_rounds):
            v1 = (v1 - (((v0 << 4) ^ (v0 >> 5)) + v0 ^ sum_val + key[(sum_val >> 11) & 3])) & 0xFFFFFFFF
            sum_val = (sum_val + delta) & 0xFFFFFFFF
            v0 = (v0 - (((v1 << 4) ^ (v1 >> 5)) + v1 ^ sum_val + key[sum_val & 3])) & 0xFFFFFFFF

        result.extend(struct.pack('<II', v0, v1))

    return bytes(result)


def xtea_encrypt(data: bytes, key: tuple[int, int, int, int]) -> bytes:
    """
    Encrypt data using XTEA algorithm (as used in OT protocol).

    Args:
        data: Plain data (will be padded to multiple of 8 bytes)
        key: Tuple of 4 uint32 values

    Returns:
        Encrypted data
    """
    # Pad to multiple of 8
    pad_len = (8 - (len(data) % 8)) % 8
    if pad_len > 0:
        data = data + b'\x00' * pad_len

    result = bytearray()
    num_rounds = 32
    delta = 0x61C88647

    for i in range(0, len(data), 8):
        v0, v1 = struct.unpack_from('<II', data, i)
        sum_val = 0

        for _ in range(num_rounds):
            v0 = (v0 + (((v1 << 4) ^ (v1 >> 5)) + v1 ^ sum_val + key[sum_val & 3])) & 0xFFFFFFFF
            sum_val = (sum_val - delta) & 0xFFFFFFFF
            v1 = (v1 + (((v0 << 4) ^ (v0 >> 5)) + v0 ^ sum_val + key[(sum_val >> 11) & 3])) & 0xFFFFFFFF

        result.extend(struct.pack('<II', v0, v1))

    return bytes(result)


def adler32_checksum(data: bytes) -> int:
    """Calculate Adler-32 checksum as used in OT protocol."""
    import zlib
    return zlib.adler32(data) & 0xFFFFFFFF
