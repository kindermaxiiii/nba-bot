import json
import os
import math


def load_json(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def phi(x):
    """
    Standard normal CDF
    """
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
