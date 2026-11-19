"""Decoder for Google/OSRM encoded polylines."""

import numpy as np
from numpy.typing import NDArray

ASCII_OFFSET = 63
CONTINUATION_BIT = 0x20
CHUNK_MASK = 0x1F


def decode(encoded: str, precision: int = 6) -> NDArray[np.float64]:
    """Return an (N, 2) array of [lon, lat] points from an encoded polyline (which stores lat first)."""
    if not encoded:
        return np.empty((0, 2))
    # Vectorised with numpy: a cross-country route has tens of thousands of points, too slow for a Python loop.
    chunks = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - ASCII_OFFSET
    is_last = chunks < CONTINUATION_BIT
    value_of_chunk = np.concatenate(([0], np.cumsum(is_last)[:-1]))
    starts = np.concatenate(([0], np.nonzero(is_last)[0][:-1] + 1))
    shift = 5 * (np.arange(len(chunks)) - starts[value_of_chunk])
    raw = np.add.reduceat((chunks & CHUNK_MASK) << shift, starts)
    deltas = ((raw >> 1) ^ -(raw & 1)).reshape(-1, 2)
    lat_lon = np.cumsum(deltas, axis=0) / 10**precision
    return lat_lon[:, ::-1].copy()
