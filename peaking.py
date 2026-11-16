"""Focus peaking overlay and a numeric sharpness score for live view frames."""

import numpy as np
from PIL import Image

# Edge-strength thresholds (0-255 brightness steps across ~2 pixels).
# Lower threshold = more pixels highlighted.
SENSITIVITY = {"Low": 70, "Medium": 45, "High": 28}
PEAK_COLOR = (255, 40, 40)


def _gray(img):
    return np.asarray(img.convert("L"), dtype=np.float32)


def _blur(g):
    """3x3 box blur, to keep JPEG noise from looking like detail."""
    p = np.pad(g, 1, mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
            p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
            p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def edge_strength(g):
    """Gradient magnitude (same size as g, zero on the border)."""
    b = _blur(g)
    mag = np.zeros_like(b)
    gx = b[1:-1, 2:] - b[1:-1, :-2]
    gy = b[2:, 1:-1] - b[:-2, 1:-1]
    mag[1:-1, 1:-1] = np.abs(gx) + np.abs(gy)
    return mag


def overlay(img, sensitivity="Medium", threshold_scale=1.0):
    """Return a copy of img with in-focus edges painted in PEAK_COLOR."""
    mag = edge_strength(_gray(img))
    mask = mag > SENSITIVITY[sensitivity] * threshold_scale
    arr = np.array(img.convert("RGB"))
    arr[mask] = PEAK_COLOR
    return Image.fromarray(arr)


def sharpness(img, box=None):
    """Sharpness score for img (optionally only inside box = (x0, y0, x1, y1)).

    Mean squared edge strength (a "Tenengrad" focus measure). Higher is sharper.
    The absolute number depends on the scene and lighting, so compare it while
    adjusting focus on the same subject: the peak is the sharpest focus.
    """
    if box is not None:
        img = img.crop(tuple(int(v) for v in box))
    mag = edge_strength(_gray(img))[1:-1, 1:-1]
    if mag.size == 0:
        return 0.0
    return float(np.mean(mag * mag)) / 10.0
