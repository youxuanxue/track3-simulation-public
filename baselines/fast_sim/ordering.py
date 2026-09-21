"""Small array-ordering helpers shared by native and fallback trace paths.

Keep this module free of pandas and ABIDES imports: native cold-start output
conversion only needs the stable two-key ordering primitive.
"""

from __future__ import annotations

import numpy as np


def stable_lexsort(primary: np.ndarray, secondary: np.ndarray) -> np.ndarray:
    """Return indices stable-sorted by ``primary`` then ``secondary``."""
    idx = np.argsort(secondary, kind="stable")
    return idx[np.argsort(primary[idx], kind="stable")]
