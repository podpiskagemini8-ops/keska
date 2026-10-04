"""Usage: python tools/verify.py giveaway-1-proof.json [original_commitment]."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rafflebot.draw import canonical, commitment, weighted_draw


def verify(proof, original_commitment=None):
    if proof["version"] != 1 or proof["algorithm"] != "sha256-integer-weighted-without-replacement-v1":
        raise ValueError("Unknown proof format")
    if commitment(proof["seed"]) != proof["commitment"]:
        raise ValueError("Seed commitment mismatch")
    if original_commitment and original_commitment != proof["commitment"]:
        raise ValueError("Commitment differs from the original channel post")
    if hashlib.sha256(canonical(proof["eligible"])).hexdigest() != proof["snapshot_sha256"]:
        raise ValueError("Participant snapshot hash mismatch")
    if weighted_draw(proof["eligible"], proof["requested_winners"], proof["seed"]) != proof["winners"]:
        raise ValueError("Winner list mismatch")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    try:
        proof = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        verify(proof, sys.argv[2] if len(sys.argv) > 2 else None)
        print("OK: ключ, список билетов и победители совпадают.")
        if len(sys.argv) < 3:
            print("Сравните отпечаток ключа с оригинальным постом: " + proof["commitment"])
    except (ValueError, KeyError, OSError) as e:
        print("FAILED: " + str(e))
        sys.exit(1)
