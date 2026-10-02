#!/usr/bin/env python3
"""
CYTHANX MOG — STRATEGIC FUSION ENGINE
===================

One program with explicit cryptographic layers:

    source bytes
        -> SHA256
        -> Enigma-inspired transform
        -> BLAKE3-256 fusion root
        -> SHA256d fusion state
        -> CYTHANIZE / Scrypt commitment
        -> JOATT state
        -> optional blockchain header
        -> Scrypt
        -> SHA256d final block hash

An optional XMRig supervisor is included as an external miner interface.
The auxiliary CYTHANX layers do NOT replace or impersonate RandomX/XMRig.

No wallet, password, token, or pool credential is embedded.
"""

from __future__ import annotations
import argparse, hashlib, hmac, json, math, os, secrets, signal, subprocess, threading, time, unicodedata, urllib.request, urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from blake3 import blake3 as _blake3
except ImportError:
    _blake3 = None


# ---------------------------------------------------------------------------
# CORE HASH LAYERS
# ---------------------------------------------------------------------------

def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def cyx_root(core: bytes) -> bytes:
    """Domain-separated CYX root function for the canonical core string."""
    return blake3_256(b"CYTHANX|ROOT|" + core)

def cyx_core_string(*parts: bytes | str) -> tuple[bytes, bytes]:
    """Build one canonical CYX core string and jam its root into that string."""
    body = b"|".join(
        p if isinstance(p, bytes) else str(p).encode()
        for p in parts
    )
    root = cyx_root(body)
    core = body + b"|ROOT|" + root.hex().encode()
    return core, root

def xmrig_seed_from_core(core: bytes) -> bytes:
    """Grow the canonical CYX core into a deterministic XMRig session seed.

    This is a CYX-side seed/commitment. It does not replace or alter RandomX
    internals; XMRig remains the external RandomX execution engine.
    """
    return blake3_256(b"CYTHANX|XMRIG-SEED|" + len(core).to_bytes(8, "big") + core)

def xmrig_seed_hex(core: bytes) -> str:
    return xmrig_seed_from_core(core).hex()

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def sha256d(data: bytes) -> bytes:
    return sha256(sha256(data))

def blake3_256(data: bytes) -> bytes:
    if _blake3 is not None:
        return _blake3(data).digest(length=32)
    return hashlib.blake2b(b"CYTHANX|BLAKE3-FALLBACK|" + data, digest_size=32).digest()

def scrypt256(data: bytes, salt: bytes | None = None) -> bytes:
    return hashlib.scrypt(
        password=data, salt=data if salt is None else salt,
        n=1024, r=1, p=1, dklen=32
    )

def pbkdf2_sha512(data: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", data, salt, 2048, 64)

def joaat(data: bytes) -> int:
    h = 0
    for byte in data:
        h = (h + byte) & 0xffffffff
        h = (h + ((h << 10) & 0xffffffff)) & 0xffffffff
        h ^= h >> 6
    h = (h + ((h << 3) & 0xffffffff)) & 0xffffffff
    h ^= h >> 11
    h = (h + ((h << 15) & 0xffffffff)) & 0xffffffff
    return h & 0xffffffff


# ---------------------------------------------------------------------------
# ENIGMA-INSPIRED AUXILIARY TRANSFORM
# ---------------------------------------------------------------------------

def _rotl8(x: int, n: int) -> int:
    n &= 7
    return ((x << n) | (x >> (8 - n))) & 0xff

def enigma_bytes(data: bytes, key: bytes) -> bytes:
    if len(key) < 32:
        raise ValueError("Enigma key must be at least 32 bytes")
    out = bytearray(len(data))
    for block_start in range(0, len(data), 32):
        block = data[block_start:block_start + 32]
        counter = block_start // 32
        stream = sha256(b"CYTHANX|ENIGMA|STREAM|" + key + counter.to_bytes(8, "big"))
        for j, byte in enumerate(block):
            k = stream[j]
            x = (byte + k + ((block_start + j) * 17)) & 0xff
            x ^= (key[(counter + j) % len(key)] + j * 29) & 0xff
            x = _rotl8(x, (k ^ key[j % len(key)]) & 7)
            out[block_start + j] = x
    return bytes(out)


# ---------------------------------------------------------------------------
# CYTHAN / JOATT LAYERS
# ---------------------------------------------------------------------------

VECTOR_KEY = b"CYTHAN-MAINNET-V1"
CHAIN_ID = "CYTHANX-MEGA-FUSION"

def cythan_vector() -> dict:
    vector = blake3_256(b"CYTHAN|VECTOR|" + CHAIN_ID.encode() + b"|" + VECTOR_KEY)
    return {"vector": vector, "joaat": joaat(vector)}

def cythanize(payload: bytes, previous: bytes = b"\0" * 32) -> dict:
    v = cythan_vector()
    vector, j = v["vector"], v["joaat"]
    jb = j.to_bytes(4, "big")
    state = blake3_256(b"CYTHAN|STATE|" + payload + previous + vector + jb)
    sha_state = sha256d(b"CYTHAN|SHA256D|" + state)
    scrypt_state = scrypt256(b"CYTHAN|SCRYPT|" + sha_state)
    commitment = blake3_256(
        b"CYTHAN|COMMITMENT|" + vector + jb + state + sha_state + scrypt_state
    )
    return {
        "vector": vector.hex(), "joaat": f"{j:08x}",
        "state": state.hex(), "sha256d": sha_state.hex(),
        "scrypt": scrypt_state.hex(), "commitment": commitment.hex()
    }

def joatt(payload: bytes, previous_state: bytes = b"\0" * 32) -> dict:
    b3_state = blake3_256(b"JOATT|BLAKE3|" + CHAIN_ID.encode() + previous_state + payload)
    root = sha256d(b"JOATT|ROOT|" + previous_state + b3_state + payload)
    pbkdf = pbkdf2_sha512(root, b"JOATT|PBKDF2|" + CHAIN_ID.encode())
    scrypt_state = scrypt256(pbkdf, b"JOATT|SCRYPT|" + CHAIN_ID.encode())
    mixed = blake3_256(b"JOATT|MIX|" + b3_state + root + pbkdf + scrypt_state)
    checksum = blake3_256(b"JOATT|CHECKSUM|" + mixed + root + scrypt_state)[:4]
    identifier64 = blake3_256(
        b"JOATT|ID64|" + checksum + mixed + previous_state
    )[:8]
    state256 = blake3_256(
        b"JOATT|STATE256|" + b3_state + root + pbkdf + scrypt_state +
        mixed + checksum + identifier64
    )
    return {
        "blake3": b3_state.hex(), "root_sha256d": root.hex(),
        "pbkdf2": pbkdf.hex(), "scrypt": scrypt_state.hex(),
        "mix": mixed.hex(), "checksum": checksum.hex(),
        "identifier64": identifier64.hex(), "state256": state256.hex()
    }

def transmogrify(payload: bytes, rounds: int = 3) -> bytes:
    x = payload
    for i in range(rounds):
        local_key = sha256(b"CYTHANX|LOCAL-ROTOR|" + i.to_bytes(4, "big"))
        x = enigma_bytes(x, local_key)
        x = blake3_256(b"CYTHANX|TRANSMOGRIFY|" + bytes([i]) + x)
        x = scrypt256(x, f"round-{i}".encode())
        x = sha256d(b"CYTHANX|SHA256D|ROUND|" + x)
    return x


# ---------------------------------------------------------------------------
# SOURCE FUSION
# ---------------------------------------------------------------------------

def digest_file(path: Path) -> dict:
    raw = path.read_bytes()
    return {"name": str(path), "bytes": len(raw), "sha256": sha256(raw).hex()}

def fuse_files(paths: list[Path], seed: str | None = None) -> dict:
    key = sha256(seed.encode()) if seed else secrets.token_bytes(32)
    chunks, records = [], []
    for path in paths:
        if not path.exists():
            records.append({"name": str(path), "status": "missing"})
            continue
        raw = path.read_bytes()
        transformed = enigma_bytes(raw, key)
        actual = sha256(raw)
        records.append({
            "name": str(path), "status": "ok", "bytes": len(raw),
            "sha256": actual.hex(), "enigma_sha256": sha256(transformed).hex()
        })
        chunks.append(path.name.encode() + b"\0" + len(raw).to_bytes(8, "big") + actual + transformed)
    envelope = b"CYTHANX|MEGA-FUSION|V2|" + b"".join(chunks)
    root = blake3_256(envelope)
    state = sha256d(b"CYTHANX|FUSION-SHA256D|" + root)
    commitment = blake3_256(b"CYTHANX|FUSION-COMMIT|" + root + state + key)
    return {
        "version": 2, "source_count": len(paths), "key_hex": key.hex(),
        "records": records, "fusion_root": root.hex(),
        "fusion_sha256d": state.hex(), "fusion_commitment": commitment.hex()
    }


# ---------------------------------------------------------------------------
# LAYERED BLOCK ENGINE
# ---------------------------------------------------------------------------

# Compact-difficulty encoding used by the canonical header.  The target is
# derived from nBits, so the field that is actually committed in the 80-byte
# header also controls proof-of-work validation.
DEFAULT_NBITS = 0x1E0FFFFF

def compact_to_target(nbits: int) -> int:
    if not 0 <= nbits <= 0xFFFFFFFF:
        raise ValueError("nBits must fit uint32")
    exponent = (nbits >> 24) & 0xFF
    mantissa = nbits & 0x007FFFFF
    if mantissa == 0:
        raise ValueError("nBits mantissa must be non-zero")
    if nbits & 0x00800000:
        raise ValueError("negative compact targets are not valid")
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))
    if target <= 0 or target >= 1 << 256:
        raise ValueError("nBits decodes to an invalid 256-bit target")
    return target

TARGET = compact_to_target(DEFAULT_NBITS)

# CYTHANX canonical 80-byte block header.
#
# Serialization is consensus-critical and deliberately independent of JSON:
#   version       uint32 LE   4 bytes
#   prev_hash     raw digest   32 bytes
#   merkle_root   raw digest   32 bytes
#   timestamp     uint32 LE   4 bytes
#   nBits         uint32 LE   4 bytes
#   nonce         uint32 LE   4 bytes
#                               --------
#                               80 bytes
#
# Hash fields are serialized as their 32-byte digest in natural byte order
# (the same order produced by hashlib.digest()). They are NOT reversed for
# display/serialization. Integer fields are unsigned little-endian.
# The payload is UTF-8 encoded and forms a single-leaf Merkle tree: its leaf
# hash is SHA256d(payload_bytes), which is therefore the Merkle root.

HEADER_SIZE = 80
HEADER_VERSION = 4

@dataclass
class Block:
    height: int
    timestamp: int
    previous_hash: str
    payload: str
    nonce: int = 0
    nbits: int = DEFAULT_NBITS
    version: int = HEADER_VERSION
    hash: str = ""

    def _hash32(self, value: str, field: str) -> bytes:
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be hexadecimal") from exc
        if len(raw) != 32:
            raise ValueError(f"{field} must contain exactly 32 bytes / 64 hex characters")
        return raw

    def payload_bytes(self) -> bytes:
        return self.payload.encode("utf-8")

    def merkle_root(self) -> bytes:
        # Single-leaf Merkle tree. Multi-transaction Merkle construction can
        # replace this later without changing the 80-byte header layout.
        return sha256d(self.payload_bytes())

    def header_bytes(self) -> bytes:
        if not 0 <= self.version <= 0xFFFFFFFF:
            raise ValueError("version must fit uint32")
        if not 0 <= self.timestamp <= 0xFFFFFFFF:
            raise ValueError("timestamp must fit uint32")
        if not 0 <= self.nbits <= 0xFFFFFFFF:
            raise ValueError("nBits must fit uint32")
        if not 0 <= self.nonce <= 0xFFFFFFFF:
            raise ValueError("nonce must fit uint32")

        header = b"".join((
            self.version.to_bytes(4, "little"),
            self._hash32(self.previous_hash, "previous_hash"),
            self.merkle_root(),
            self.timestamp.to_bytes(4, "little"),
            self.nbits.to_bytes(4, "little"),
            self.nonce.to_bytes(4, "little"),
        ))
        if len(header) != HEADER_SIZE:
            raise AssertionError(f"CYTHANX header must be {HEADER_SIZE} bytes")
        return header

    def header(self) -> dict:
        # Human-readable representation; NEVER used for consensus hashing.
        return {
            "version": self.version,
            "height": self.height,
            "previous_hash": self.previous_hash,
            "merkle_root": self.merkle_root().hex(),
            "timestamp": self.timestamp,
            "nBits": f"{self.nbits:08x}",
            "nonce": self.nonce,
            "header_hex": self.header_bytes().hex(),
        }

    def layered_hash(self) -> bytes:
        header = self.header_bytes()
        # Layer order is deliberately explicit and consensus-critical.
        stage1 = blake3_256(b"CYTHANX|HEADER|" + header)
        stage2 = scrypt256(stage1, salt=header)
        stage3 = sha256d(b"CYTHANX|POW|SHA256D|" + stage2)
        return stage3

    def mine(self, target: int | None = None, max_nonce: int | None = None) -> bool:
        # By default, proof-of-work difficulty is derived from the nBits field
        # committed inside this exact header. An explicit target remains
        # available for test vectors and controlled experiments.
        target_value = compact_to_target(self.nbits) if target is None else target
        if not 0 < target_value < (1 << 256):
            raise ValueError("target must be in the range 1..2^256-1")
        if max_nonce is not None:
            if not 0 <= max_nonce <= 0xFFFFFFFF:
                raise ValueError("max_nonce must fit uint32")
            if self.nonce > max_nonce:
                return False
        while self.nonce <= 0xFFFFFFFF:
            digest = self.layered_hash()
            if int.from_bytes(digest, "big") <= target_value:
                self.hash = digest.hex()
                return True
            if max_nonce is not None and self.nonce >= max_nonce:
                return False
            if self.nonce == 0xFFFFFFFF:
                return False
            self.nonce += 1
        return False


# ---------------------------------------------------------------------------
# ORGANISM/TELEMETRY STATE
# ---------------------------------------------------------------------------

@dataclass
class OrganismState:
    energy: float = 1.0
    entropy: float = 0.0
    health: float = 1.0
    generation: int = 0

    def adapt(self, signal_value: bytes) -> None:
        x = int.from_bytes(blake3_256(signal_value)[:8], "big") / 2**64
        self.entropy = -math.log2(max(x, 2**-64)) / 64.0
        self.energy = max(0.0, min(2.0, self.energy * (1.0 + 0.02 + 0.08*x)))
        self.health = max(0.0, min(1.0, 0.97*self.health + 0.03*(1.0-abs(x-0.5))))
        self.generation += 1

def organism(seed: bytes, rounds: int = 3) -> dict:
    if rounds < 0:
        raise ValueError("organism rounds must be non-negative")
    state, previous = blake3_256(b"CYTHANX|GENESIS|" + seed), b""
    org = OrganismState()
    for i in range(rounds):
        payload = enigma_bytes(state + previous, sha256(seed + i.to_bytes(4, "big")))
        jo = joatt(payload, previous)
        vec = cythanize(bytes.fromhex(jo["state256"]), previous)
        transformed = transmogrify(bytes.fromhex(vec["commitment"]), 3 + i % 3)
        aux = blake3_256(b"CYTHANX|AUX|" + transformed)[:16]
        core_string, root = cyx_core_string(
            b"CYTHANX", b"CORE",
            state,
            bytes.fromhex(jo["state256"]),
            bytes.fromhex(vec["commitment"]),
            transformed,
            aux,
            i.to_bytes(8, "big"),
        )
        state = blake3_256(b"CYTHANX|STATE|" + core_string)
        org.adapt(root)
        previous = bytes.fromhex(jo["state256"])
    return {"state256": state.hex(), "organism": asdict(org)}


# ---------------------------------------------------------------------------
# XMRIG MEGA-FUSION SUPERVISOR
# ---------------------------------------------------------------------------
# XMRig remains an external mining engine. CYTHANX owns the orchestration,
# telemetry, lifecycle, fusion/organism state, and restart policy. The actual
# proof-of-work algorithm is whatever XMRig is configured to run (rx/0 by
# default); this module does not claim that XMRig natively implements the
# CYTHANX layered hash.

STOP = threading.Event()
PROCESS: subprocess.Popen | None = None


def _api_json(args: argparse.Namespace, path: str) -> dict | None:
    """Read a local XMRig HTTP API endpoint without external dependencies."""
    url = f"http://127.0.0.1:{args.api_port}{path}"
    req = urllib.request.Request(url)
    if getattr(args, "api_token", None):
        req.add_header("Authorization", f"Bearer {args.api_token}")
    try:
        with urllib.request.urlopen(req, timeout=2.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def xmrig_command(args: argparse.Namespace) -> list[str]:
    threads = args.threads or max(1, os.cpu_count() or 1)
    if not args.user:
        raise ValueError("--user is required for XMRig mode")

    cmd = [
        args.xmrig_path,
        "--algo", args.algo,
        "--url", args.pool,
        "--user", args.user,
        "--pass", args.password,
        "--threads", str(threads),
        "--http-host", "127.0.0.1",
        "--http-port", str(args.api_port),
        "--print-time", str(args.print_time),
    ]
    if getattr(args, "api_token", None):
        cmd += ["--http-access-token", args.api_token]
    if getattr(args, "rig_id", None):
        cmd += ["--rig-id", args.rig_id]
    if getattr(args, "keepalive", False):
        cmd += ["--keepalive"]
    if getattr(args, "tls", False):
        cmd += ["--tls"]
    return cmd


def write_xmrig_config(args: argparse.Namespace, path: Path) -> Path:
    """Emit a portable XMRig config matching the supervisor settings."""
    threads = args.threads or max(1, os.cpu_count() or 1)
    config = {
        "autosave": True,
        "background": False,
        "colors": True,
        "print-time": args.print_time,
        "health-print-time": args.print_time,
        "donate-level": 1,
        "http": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": args.api_port,
            "access-token": args.api_token or None,
            "restricted": True,
        },
        "pools": [{
            "algo": args.algo,
            "url": args.pool,
            "user": args.user or "SET_YOUR_WALLET_OR_POOL_USER",
            "pass": args.password,
            "rig-id": args.rig_id or None,
            "keepalive": bool(args.keepalive),
            "tls": bool(args.tls),
            "enabled": True,
        }],
        "cpu": {
            "enabled": True,
            "max-threads-hint": 100,
            "threads": threads,
        },
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def xmrig_status(args: argparse.Namespace) -> dict:
    summary = _api_json(args, "/1/summary") or {}
    hashrate = summary.get("hashrate", {}) if isinstance(summary, dict) else {}
    results = summary.get("results", {}) if isinstance(summary, dict) else {}
    return {
        "worker": getattr(args, "rig_id", None) or getattr(args, "user", None),
        "algo": args.algo,
        "pool": args.pool,
        "uptime": summary.get("uptime"),
        "hashrate": hashrate,
        "accepted": results.get("shares_good", results.get("accepted")),
        "rejected": results.get("shares_total", results.get("rejected")),
        "raw": summary,
    }


def stop_xmrig() -> None:
    global PROCESS
    if PROCESS is None or PROCESS.poll() is not None:
        PROCESS = None
        return
    try:
        PROCESS.terminate()
        PROCESS.wait(timeout=10)
    except Exception:
        try:
            PROCESS.kill()
        except Exception:
            pass
    PROCESS = None


def _xmrig_banner(args: argparse.Namespace, generation: int) -> None:
    print("=" * 72)
    print(" CYTHANX MEGA FUSION // XMRIG ORGANISM ENGINE")
    print(f" ALGORITHM : {args.algo}")
    print(f" POOL      : {args.pool}")
    print(f" WORKER    : {args.rig_id or args.user}")
    print(f" GENERATION: {generation}")
    print(" CYTHANX   : FUSION -> JOATT -> ORGANISM -> MINER -> TELEMETRY")
    print("=" * 72)


def run_xmrig(args: argparse.Namespace) -> int:
    global PROCESS
    STOP.clear()

    def shutdown(signum, frame):
        STOP.set()
        stop_xmrig()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    # Grow the canonical CYX core into the deterministic XMRig session seed.
    session_seed = sha256((args.seed or "CYTHANX-MEGA-FUSION").encode())
    seed_org = organism(session_seed, rounds=max(1, args.organism_rounds))
    generation = seed_org["organism"]["generation"]
    seed_core, seed_root = cyx_core_string(
        b"CYTHANX", b"XMRIG-SEED", session_seed,
        bytes.fromhex(seed_org["state256"]), generation.to_bytes(8, "big"),
    )
    xmrig_seed = xmrig_seed_from_core(seed_core)
    _xmrig_banner(args, generation)
    print(f"[CYTHANX] XMRig seed: {xmrig_seed.hex()}")

    if getattr(args, "write_config", None):
        config_path = write_xmrig_config(args, Path(args.write_config))
        print(f"[CYTHANX] XMRig config written: {config_path}")

    restart_count = 0
    while not STOP.is_set():
        try:
            cmd = xmrig_command(args)
            print(f"[CYTHANX/XMRIG] starting (restart={restart_count})")
            PROCESS = subprocess.Popen(cmd)
            last_status = 0.0

            while not STOP.is_set():
                if PROCESS.poll() is not None:
                    print(f"[CYTHANX/XMRIG] process exited with code {PROCESS.returncode}")
                    break

                now = time.monotonic()
                if now - last_status >= args.status_interval:
                    status = xmrig_status(args)
                    # Feed miner telemetry into the organism without changing
                    # XMRig's own PoW semantics.
                    signal_bytes = canonical({
                        "generation": generation,
                        "xmrig_seed": xmrig_seed.hex(),
                        "status": status,
                    })
                    org = organism(sha256(signal_bytes), rounds=1)
                    print(json.dumps({
                        "event": "miner_telemetry",
                        "generation": generation,
                        "organism": org["organism"],
                        "miner": {
                            "uptime": status.get("uptime"),
                            "hashrate": status.get("hashrate"),
                            "accepted": status.get("accepted"),
                            "rejected": status.get("rejected"),
                        },
                    }, separators=(",", ":")))
                    generation += 1
                    last_status = now
                time.sleep(0.5)

            if STOP.is_set():
                break
            restart_count += 1
            time.sleep(max(1, args.restart_delay))
        except FileNotFoundError:
            print(f"[CYTHANX/XMRIG] XMRig executable not found: {args.xmrig_path}")
            return 2
        except Exception as exc:
            print(f"[CYTHANX/XMRIG] supervisor error: {exc}")
            if STOP.is_set():
                break
            time.sleep(max(1, args.restart_delay))

    stop_xmrig()
    print("[CYTHANX/XMRIG] supervisor stopped cleanly")
    return 0



# ---------------------------------------------------------------------------
# BIP-39 / BIP-32 MNEMONIC DERIVATION BRIDGE
# ---------------------------------------------------------------------------
# This section implements the workflow represented by the supplied
# Transmogrification System diagram:
#   entropy -> SHA-256 checksum -> 11-bit words -> PBKDF2-HMAC-SHA512
#   -> BIP-32 master key -> BIP-44 Bitcoin address.
#
# The official 2048-word BIP-39 English list is deliberately NOT embedded.
# Pass its local path with --bip39-wordlist. This avoids silently substituting
# a non-standard word list.

SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _nfkd(text: str) -> str:
    return unicodedata.normalize("NFKD", text)


def load_bip39_wordlist(path: Path) -> list[str]:
    words = [_nfkd(x.strip()) for x in path.read_text(encoding="utf-8").splitlines()
             if x.strip()]
    if len(words) != 2048 or len(set(words)) != 2048:
        raise ValueError("BIP-39 English wordlist must contain exactly 2048 unique words")
    return words


def bip39_entropy_to_mnemonic(entropy: bytes, words: list[str]) -> str:
    if len(entropy) not in (16, 20, 24, 28, 32):
        raise ValueError("BIP-39 entropy must be 128, 160, 192, 224, or 256 bits")
    checksum_bits = len(entropy) * 8 // 32
    digest = hashlib.sha256(entropy).digest()
    bits = int.from_bytes(entropy, "big") << checksum_bits
    bits |= int.from_bytes(digest, "big") >> (256 - checksum_bits)
    total = len(entropy) * 8 + checksum_bits
    count = total // 11
    return " ".join(words[(bits >> (total - 11 * (i + 1))) & 0x7ff] for i in range(count))


def bip39_mnemonic_to_entropy(mnemonic: str, words: list[str]) -> bytes:
    normalized = _nfkd(" ".join(mnemonic.strip().split()))
    parts = normalized.split(" ")
    if len(parts) not in (12, 15, 18, 21, 24):
        raise ValueError("BIP-39 mnemonic must contain 12, 15, 18, 21, or 24 words")
    index = {word: i for i, word in enumerate(words)}
    try:
        values = [index[w] for w in parts]
    except KeyError as exc:
        raise ValueError(f"word is not in the supplied BIP-39 wordlist: {exc.args[0]!r}") from exc
    total = len(parts) * 11
    stream = 0
    for value in values:
        stream = (stream << 11) | value
    checksum_bits = total // 33
    entropy_bits = total - checksum_bits
    entropy = (stream >> checksum_bits).to_bytes(entropy_bits // 8, "big")
    supplied = stream & ((1 << checksum_bits) - 1)
    expected = int.from_bytes(hashlib.sha256(entropy).digest(), "big") >> (256 - checksum_bits)
    if supplied != expected:
        raise ValueError("BIP-39 checksum mismatch")
    return entropy


def bip39_seed(mnemonic: str, passphrase: str = "") -> bytes:
    password = _nfkd(mnemonic).encode("utf-8")
    salt = ("mnemonic" + _nfkd(passphrase)).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", password, salt, 2048, 64)


def _inv(x: int, p: int = SECP256K1_P) -> int:
    return pow(x, p - 2, p)


def _point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if a == b:
        m = (3 * x1 * x1) * _inv(2 * y1) % SECP256K1_P
    else:
        m = (y2 - y1) * _inv(x2 - x1) % SECP256K1_P
    x3 = (m * m - x1 - x2) % SECP256K1_P
    y3 = (m * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def _point_mul(k: int, point=(SECP256K1_GX, SECP256K1_GY)):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def secp256k1_pubkey(private_key: bytes, compressed: bool = True) -> bytes:
    k = int.from_bytes(private_key, "big")
    if not 1 <= k < SECP256K1_N:
        raise ValueError("invalid secp256k1 private key")
    x, y = _point_mul(k)
    if compressed:
        return bytes([2 | (y & 1)]) + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def bip32_master(seed: bytes) -> tuple[bytes, bytes]:
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key, chain = i[:32], i[32:]
    if not 1 <= int.from_bytes(key, "big") < SECP256K1_N:
        raise ValueError("invalid BIP-32 master key")
    return key, chain


def bip32_ckd_priv(key: bytes, chain: bytes, index: int) -> tuple[bytes, bytes]:
    if not 0 <= index <= 0xffffffff:
        raise ValueError("BIP-32 child index out of range")
    if index >= 0x80000000:
        data = b"\x00" + key + index.to_bytes(4, "big")
    else:
        data = secp256k1_pubkey(key) + index.to_bytes(4, "big")
    i = hmac.new(chain, data, hashlib.sha512).digest()
    child = (int.from_bytes(i[:32], "big") + int.from_bytes(key, "big")) % SECP256K1_N
    if int.from_bytes(i[:32], "big") >= SECP256K1_N or child == 0:
        raise ValueError("BIP-32 invalid child; retry with next valid derivation index")
    return child.to_bytes(32, "big"), i[32:]


def bip32_derive_path(key: bytes, chain: bytes, path: str) -> tuple[bytes, bytes]:
    if path in ("m", ""):
        return key, chain
    if not path.startswith("m/"):
        raise ValueError("BIP-32 path must start with m/")
    for component in path[2:].split("/"):
        hardened = component.endswith("'") or component.endswith("h")
        number = component[:-1] if hardened else component
        if not number.isdigit():
            raise ValueError(f"invalid BIP-32 path component: {component}")
        index = int(number)
        if index >= 0x80000000:
            raise ValueError("non-hardened index exceeds BIP-32 range")
        if hardened:
            index |= 0x80000000
        key, chain = bip32_ckd_priv(key, chain, index)
    return key, chain


def base58_encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return B58[0] * (len(raw) - len(raw.lstrip(b"\0"))) + (out or "")


def base58check(payload: bytes) -> str:
    return base58_encode(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])


def bip32_xprv(key: bytes, chain: bytes, depth: int, parent_fp: bytes, child_index: int) -> str:
    payload = (b"\x04\x88\xad\xe4" + bytes([depth]) + parent_fp +
               child_index.to_bytes(4, "big") + chain + b"\x00" + key)
    return base58check(payload)


def bip32_xpub(key: bytes, chain: bytes, depth: int, parent_fp: bytes, child_index: int) -> str:
    pub = secp256k1_pubkey(key)
    payload = (b"\x04\x88\xb2\x1e" + bytes([depth]) + parent_fp +
               child_index.to_bytes(4, "big") + chain + pub)
    return base58check(payload)


def hash160(data: bytes) -> bytes:
    try:
        ripe = hashlib.new("ripemd160")
    except ValueError as exc:
        raise RuntimeError("Python/OpenSSL RIPEMD160 is required for Bitcoin P2PKH") from exc
    ripe.update(hashlib.sha256(data).digest())
    return ripe.digest()


def bitcoin_p2pkh(pubkey: bytes) -> str:
    # Mainnet P2PKH: version 0x00 + HASH160(compressed public key) + checksum.
    return base58check(b"\x00" + hash160(pubkey))


def bip39_derive_report(mnemonic: str, passphrase: str, words: list[str],
                        path: str = "m/44'/0'/0'/0/0") -> dict:
    entropy = bip39_mnemonic_to_entropy(mnemonic, words)
    seed = bip39_seed(mnemonic, passphrase)
    master_key, master_chain = bip32_master(seed)
    child_key, child_chain = bip32_derive_path(master_key, master_chain, path)

    # BIP-32 metadata for the selected path.
    components = path[2:].split("/") if path != "m" else []
    parent_key, parent_chain = bip32_derive_path(master_key, master_chain,
                                                  "m" if len(components) <= 1 else
                                                  "m/" + "/".join(components[:-1]))
    parent_pub = secp256k1_pubkey(parent_key)
    parent_fp = hash160(parent_pub)[:4]
    child_index = 0 if not components else (
        int(components[-1].rstrip("'h")) |
        (0x80000000 if components[-1].endswith(("'", "h")) else 0)
    )
    pub = secp256k1_pubkey(child_key)
    return {
        "entropy_hex": entropy.hex(),
        "entropy_bits": len(entropy) * 8,
        "checksum_bits": len(entropy) * 8 // 32,
        "mnemonic_word_count": len(mnemonic.split()),
        "pbkdf2": "PBKDF2-HMAC-SHA512",
        "iterations": 2048,
        "seed_512_hex": seed.hex(),
        "master_private_key_hex": master_key.hex(),
        "master_chain_code_hex": master_chain.hex(),
        "path": path,
        "child_private_key_hex": child_key.hex(),
        "child_chain_code_hex": child_chain.hex(),
        "child_public_key_hex": pub.hex(),
        "bitcoin_p2pkh": bitcoin_p2pkh(pub),
        "master_xprv": bip32_xprv(master_key, master_chain, 0, b"\0"*4, 0),
        "master_xpub": bip32_xpub(master_key, master_chain, 0, b"\0"*4, 0),
        "derived_xprv": bip32_xprv(child_key, child_chain, len(components),
                                    parent_fp, child_index),
        "derived_xpub": bip32_xpub(child_key, child_chain, len(components),
                                    parent_fp, child_index),
    }


def bip39_generate_report(words: list[str], passphrase: str = "",
                           path: str = "m/44'/0'/0'/0/0") -> dict:
    entropy = secrets.token_bytes(32)  # 24 words / 256 bits
    mnemonic = bip39_entropy_to_mnemonic(entropy, words)
    report = bip39_derive_report(mnemonic, passphrase, words, path)
    # Never expose the generated mnemonic through an implicit side effect in
    # the CYTHANX miner. It is printed only when this explicit mode is used.
    report["mnemonic"] = mnemonic
    return report


def mnemonic_fusion(mnemonic: str, passphrase: str, words: list[str]) -> dict:
    """Bridge the BIP-39 seed into the existing CYTHANX fusion pipeline."""
    seed = bip39_seed(mnemonic, passphrase)
    j = joatt(seed)
    c = cythanize(bytes.fromhex(j["state256"]))
    transformed = transmogrify(bytes.fromhex(c["commitment"]))
    root = blake3_256(b"CYTHANX|BIP39-FUSION|" + seed + transformed)
    return {
        "bip39_seed_sha256": sha256(seed).hex(),
        "joatt_state256": j["state256"],
        "cythan_commitment": c["commitment"],
        "transmogrified": transformed.hex(),
        "fusion_root": root.hex(),
    }


# ---------------------------------------------------------------------------
# CYTHANX MOG STRATEGIC CONTROL
# ---------------------------------------------------------------------------

MOG_VERSION = "1.0"

def mog_status() -> dict:
    """Return a non-secret structural status snapshot."""
    return {
        "name": "CYTHANX MOG",
        "version": MOG_VERSION,
        "architecture": [
            "validate",
            "fuse",
            "organism",
            "mine-block",
            "xmrig",
        ],
        "blake3_backend": "blake3" if _blake3 else "blake2b-fallback",
        "header_size": HEADER_SIZE,
        "default_nbits": f"{DEFAULT_NBITS:08x}",
        "xmrig_external": True,
        "wallet_or_password_embedded": False,
    }

def run_mog(args: argparse.Namespace) -> int:
    """Central strategic controller; executes one selected operating mode."""
    print(json.dumps({
        "event": "mog_start",
        "version": MOG_VERSION,
        "mode": args.procedure or "validate",
    }, indent=2))

    if args.procedure == "validate":
        print(json.dumps(self_test(), indent=2))
        return 0

    if args.procedure == "fuse":
        paths = args.fuse or [Path(__file__)]
        result = fuse_files(paths, args.seed)
        print(json.dumps(result, indent=2))
        return 0

    if args.procedure == "organism":
        seed = sha256((args.seed or "CYTHANX-MOG").encode())
        print(json.dumps(organism(seed, rounds=max(1, args.organism_rounds)), indent=2))
        return 0

    if args.procedure == "mine-block":
        seed = sha256((args.seed or "CYTHANX-MOG").encode())
        payload = cythanize(seed)["commitment"]
        block = Block(0, int(time.time()), "00" * 32, payload)
        if block.mine(max_nonce=args.max_nonce):
            print(json.dumps(asdict(block), indent=2))
            return 0
        print(json.dumps({
            "event": "bounded_mining_exhausted",
            "max_nonce": args.max_nonce,
        }, indent=2))
        return 1

    if args.procedure == "xmrig":
        return run_xmrig(args)

    if args.procedure == "auto":
        print("[CYTHANX MOG] validate -> supervisor")
        print(json.dumps(self_test(), indent=2))
        return run_xmrig(args)

    raise SystemExit("Select --procedure {validate,fuse,organism,mine-block,xmrig,auto}")


# ---------------------------------------------------------------------------
# CLI / SELF TEST
# ---------------------------------------------------------------------------

def self_test() -> dict:
    sample = b"CYTHANX|SELF-TEST"
    d = sha256d(sample)
    c = cythanize(sample)
    j = joatt(sample)
    o = organism(d, 2)
    block = Block(0, 0, "00"*32, c["commitment"])
    # Avoid an expensive proof-of-work test: verify deterministic 32-byte output.
    header = block.header_bytes()
    digest = block.layered_hash()
    assert len(header) == HEADER_SIZE == 80
    assert len(d) == len(digest) == 32
    assert len(bytes.fromhex(j["state256"])) == 32
    assert o["organism"]["generation"] == 2
    core_a, root_a = cyx_core_string(b"CYTHANX", b"CORE", sample)
    core_b, root_b = cyx_core_string(b"CYTHANX", b"CORE", sample)
    assert core_a == core_b and root_a == root_b
    assert (b"|ROOT|" + root_a.hex().encode()) in core_a
    seed_a = xmrig_seed_from_core(core_a)
    seed_b = xmrig_seed_from_core(core_b)
    assert len(seed_a) == 32 and seed_a == seed_b
    return {
        "ok": True, "sha256d": d.hex(),
        "cythan_commitment": c["commitment"],
        "joatt_state256": j["state256"],
        "layered_block_hash": digest.hex(),
        "xmrig_seed": seed_a.hex(),
        "header_size": len(header),
        "blake3_backend": "blake3" if _blake3 else "blake2b-fallback"
    }

def parse_args():
    p = argparse.ArgumentParser(description="CYTHANX MOG strategic fusion engine and operating controller")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--fuse", nargs="*", type=Path)
    p.add_argument("--seed")
    p.add_argument("--organism", action="store_true")
    p.add_argument("--mine-block", action="store_true")
    p.add_argument("--max-nonce", type=int, default=1000, help="maximum nonce for a bounded mining/debug run")
    p.add_argument("--xmrig", action="store_true", help="run the external XMRig supervisor")
    p.add_argument("--auto", action="store_true", help="Termux-style self-test then start XMRig")
    p.add_argument("--xmrig-path", default=os.environ.get("CYTHANX_XMRIG", os.environ.get("XMRIG_PATH", "xmrig")))
    p.add_argument("--pool", default=os.environ.get("CYTHANX_POOL", os.environ.get("XMRIG_POOL", "xmrig.nanswap.com:3333")))
    p.add_argument("--user", default=os.environ.get("CYTHANX_USER", os.environ.get("XMRIG_USER")))
    p.add_argument("--password", default=os.environ.get("CYTHANX_PASS", os.environ.get("XMRIG_PASS", "x")))
    p.add_argument("--algo", default=os.environ.get("CYTHANX_ALGO", os.environ.get("XMRIG_ALGO", "rx/0")))
    p.add_argument("--threads", type=int)
    p.add_argument("--api-port", type=int, default=16000)
    p.add_argument("--print-time", type=int, default=60)
    p.add_argument("--restart-delay", type=int, default=5)
    p.add_argument("--status-interval", type=int, default=15, help="XMRig telemetry interval in seconds")
    p.add_argument("--api-token", default=os.environ.get("CYTHANX_XMRIG_API_TOKEN"), help="optional XMRig HTTP API bearer token")
    p.add_argument("--rig-id", default=os.environ.get("CYTHANX_RIG_ID"), help="XMRig rig identifier")
    p.add_argument("--keepalive", action="store_true", help="request pool keepalive")
    p.add_argument("--tls", action="store_true", help="enable XMRig pool TLS")
    p.add_argument("--write-config", help="write an XMRig JSON config and continue")
    p.add_argument("--organism-rounds", type=int, default=3, help="CYTHANX organism rounds for supervisor telemetry")
    p.add_argument("--bip39-wordlist", type=Path, help="path to the official 2048-word BIP-39 English list")
    p.add_argument("--bip39-generate", action="store_true", help="generate a secure 24-word BIP-39 mnemonic and derive BIP-32/BIP-44 data")
    p.add_argument("--bip39-mnemonic", help="derive from an explicitly supplied BIP-39 mnemonic (avoid shell history for real secrets)")
    p.add_argument("--bip39-passphrase", default="", help="BIP-39 passphrase; use only for controlled/testing workflows")
    p.add_argument("--bip39-path", default="m/44'/0'/0'/0/0", help="BIP-32/BIP-44 derivation path")
    p.add_argument("--bip39-fusion", action="store_true", help="feed the BIP-39 seed through the existing CYTHANX JOATT/CYTHANIZE fusion")
    p.add_argument(
        "--procedure",
        choices=("validate", "fuse", "organism", "mine-block", "xmrig", "auto"),
        help="explicit operating procedure; equivalent to the corresponding mode",
    )
    p.add_argument("--procedures", action="store_true", help="print the operating-procedure allocation and exit")
    p.add_argument("--mog-status", action="store_true", help="print CYTHANX MOG structural status and exit")
    p.add_argument("--all", action="store_true", help="execute the complete procedure chain in one invocation")
    p.add_argument("--all-xmrig", action="store_true", help="allow --all to launch the external XMRig supervisor")
    return p.parse_args()

def print_operating_procedures() -> None:
    procedures = {
        "validate": "Run deterministic self-test; do not start a miner.",
        "fuse": "Digest and fuse supplied source files into a fusion commitment.",
        "organism": "Derive CYTHANX organism state from the selected seed.",
        "mine-block": "Construct and locally search the 80-byte CYTHANX layered block header.",
        "xmrig": "Run external XMRig under the CYTHANX supervisor and telemetry loop.",
        "auto": "Validate first, then enter the external XMRig supervisor.",
    }
    print(json.dumps({"operating_procedures": procedures}, indent=2))




def run_all_procedures(args: argparse.Namespace) -> int:
    """Single executable phrase: validate -> fuse -> organism -> mine -> XMRig."""
    import subprocess
    import sys

    commands = [
        [sys.executable, __file__, "--self-test"],
        [sys.executable, __file__, "--fuse", __file__],
        [sys.executable, __file__, "--organism", "--organism-rounds", str(max(1, args.organism_rounds))],
        [sys.executable, __file__, "--mine-block", "--max-nonce", str(args.max_nonce)],
    ]
    for i, command in enumerate(commands, 1):
        print(f"[{i}/4] {' '.join(command[2:])}")
        result = subprocess.run(command)
        # A bounded mining stage returning 1 means "no share found within
        # max_nonce"; that is a valid diagnostic outcome, not a crash.
        if result.returncode != 0:
            if i == 4 and args.max_nonce is not None:
                print("[4/5] bounded mining window exhausted; continuing chain")
            else:
                return result.returncode

    print("[5/5] XMRIG")
    if args.all_xmrig:
        return subprocess.run([sys.executable, __file__, "--xmrig"]).returncode

    print("Skipped external XMRig; pass --all-xmrig to enable it explicitly.")
    return 0

def main() -> int:
    args = parse_args()
    if args.procedures:
        print_operating_procedures()
        return 0

    if args.mog_status:
        print(json.dumps(mog_status(), indent=2))
        return 0

    if args.bip39_generate or args.bip39_mnemonic or args.bip39_fusion:
        if not args.bip39_wordlist:
            raise SystemExit("--bip39-wordlist is required for BIP-39 modes")
        try:
            words = load_bip39_wordlist(args.bip39_wordlist)
            if args.bip39_generate:
                report = bip39_generate_report(words, args.bip39_passphrase, args.bip39_path)
                print(json.dumps(report, indent=2))
                return 0
            if not args.bip39_mnemonic:
                raise SystemExit("--bip39-mnemonic is required for --bip39-fusion")
            report = bip39_derive_report(args.bip39_mnemonic, args.bip39_passphrase,
                                         words, args.bip39_path)
            if args.bip39_fusion:
                report["cythanx_fusion"] = mnemonic_fusion(args.bip39_mnemonic,
                                                           args.bip39_passphrase, words)
            print(json.dumps(report, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[BIP39] {exc}")
            return 2

    if args.all:
        return run_all_procedures(args)

    # Explicit operating-procedure allocation. Existing flags remain supported
    # for backward compatibility; --procedure is the unambiguous SOP interface.
    if args.procedure:
        return run_mog(args)

    if args.self_test:
        print(json.dumps(self_test(), indent=2))
        return 0

    if args.auto:
        # Equivalent to the supplied Termux launcher: validate first, then
        # hand off to the external XMRig supervisor.
        print("[CYTHANX] Automatic supervisor mode")
        print(json.dumps(self_test(), indent=2))
        args.xmrig = True

    if args.fuse is not None:
        result = fuse_files(args.fuse, args.seed)
        print(json.dumps(result, indent=2))
        seed = bytes.fromhex(result["fusion_commitment"])
    else:
        seed = sha256((args.seed or "CYTHANX").encode())

    if args.organism:
        print(json.dumps(organism(seed), indent=2))

    if args.mine_block:
        payload = cythanize(seed)["commitment"]
        block = Block(0, int(time.time()), "00"*32, payload)
        if block.mine(max_nonce=args.max_nonce):
            print(json.dumps(asdict(block), indent=2))
        else:
            print("Block mining stopped.")
            return 1

    if args.xmrig:
        return run_xmrig(args)

    if args.fuse is None and not args.organism and not args.mine_block:
        print(json.dumps(self_test(), indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
