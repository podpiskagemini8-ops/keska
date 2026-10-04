"""Reproducible integer-weighted sampling without replacement.

The seed commitment is published before entry opens. The final proof reveals
the seed and eligible pseudonymous entries; tools/verify.py reproduces it.
This proves reproducibility, not independence of the organizer's entrant list.
"""
import hashlib
import json
import secrets


def commitment(seed):
    return hashlib.sha256(bytes.fromhex(seed)).hexdigest()


def ticket(rid, uid):
    return hashlib.sha256(f"{rid}:{uid}".encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def weighted_draw(entries, count, seed):
    pool = sorted([dict(x) for x in entries], key=lambda x: x["ticket"])
    if len({x["ticket"] for x in pool}) != len(pool):
        raise ValueError("Duplicate ticket")
    if any(type(x["weight"]) is not int or x["weight"] <= 0 for x in pool):
        raise ValueError("Invalid weight")
    counter, winners = 0, []
    for _ in range(min(count, len(pool))):
        total = sum(x["weight"] for x in pool)
        ceiling = (1 << 256) - ((1 << 256) % total)
        while True:
            digest = hashlib.sha256(bytes.fromhex(seed) + counter.to_bytes(8, "big")).digest()
            counter += 1
            number = int.from_bytes(digest, "big")
            if number < ceiling:
                choice = number % total
                break
        for index, entry in enumerate(pool):
            if choice < entry["weight"]:
                winners.append(entry["ticket"])
                pool.pop(index)
                break
            choice -= entry["weight"]
    return winners


def new_seed():
    return secrets.token_hex(32)
