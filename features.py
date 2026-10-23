"""Timestamp-agnostic, MicroPython-friendly features for fixed sample windows.

train.py embeds this module's source in the generated model.py, so keep it to
plain MicroPython: only `math`, no star-unpacking, no numpy.
"""
import math

FEATURE_NAMES = (
    "accel_mag_mean",
    "accel_mag_std",
    "accel_mag_min",
    "accel_mag_max",
    "gyro_mag_mean",
    "gyro_mag_std",
    "accel_x_std",
    "accel_y_std",
    "accel_z_std",
    "gyro_x_std",
    "gyro_y_std",
    "gyro_z_std",
    "accel_autocorr",
    "accel_peaks",
    "accel_peak_gap_cv",
    "accel_vertical_std",
    "accel_horizontal_std",
    "accel_jerk",
)
AXIS_NAMES = ("ax", "ay", "az", "gx", "gy", "gz")
LSB_PER_G = 16384.0
LSB_PER_DPS = 70.0
# Step-period search range and peak threshold, as fractions of the window.
AUTOCORR_MIN_LAG = 0.3
AUTOCORR_MAX_LAG = 0.8
PEAK_MIN_G = 0.05


def _mean_std(values):
    n = len(values)
    if not n:
        return 0.0, 0.0
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    return mean, math.sqrt(variance)


def _rhythm(rows, mags, mag_mean, mag_std):
    """Walking is periodic: autocorrelation, peak count/regularity, and how
    much of the motion runs along gravity versus across it."""
    n = len(mags)
    dev = [m - mag_mean for m in mags]
    energy = sum(d * d for d in dev) or 1e-9
    autocorr = 0.0
    lo = max(1, int(n * AUTOCORR_MIN_LAG))
    hi = min(n - 2, int(n * AUTOCORR_MAX_LAG))
    for lag in range(lo, hi + 1):
        total = 0.0
        for i in range(n - lag):
            total += dev[i] * dev[i + lag]
        if total / energy > autocorr:
            autocorr = total / energy

    threshold = max(PEAK_MIN_G, 0.5 * mag_std)
    peaks = []
    armed = True
    for i in range(n):
        if armed and dev[i] > threshold:
            peaks.append(i)
            armed = False
        elif not armed and dev[i] < 0:
            armed = True
    gaps = [peaks[i + 1] - peaks[i] for i in range(len(peaks) - 1)]
    gap_mean, gap_std = _mean_std(gaps)
    gap_cv = gap_std / gap_mean if len(gaps) >= 2 and gap_mean else 1.0

    gx = sum(r[0] for r in rows) / n
    gy = sum(r[1] for r in rows) / n
    gz = sum(r[2] for r in rows) / n
    g = math.sqrt(gx * gx + gy * gy + gz * gz) or 1.0
    ux, uy, uz = gx / g, gy / g, gz / g
    vertical = []
    horizontal = []
    for i in range(n):
        r = rows[i]
        v = (r[0] * ux + r[1] * uy + r[2] * uz) / LSB_PER_G
        vertical.append(v)
        horizontal.append(math.sqrt(max(0.0, mags[i] * mags[i] - v * v)))
    jerk = 0.0
    for i in range(n - 1):
        jerk += abs(mags[i + 1] - mags[i])
    jerk = jerk / (n - 1) if n > 1 else 0.0
    return (autocorr, float(len(peaks)), gap_cv,
            _mean_std(vertical)[1], _mean_std(horizontal)[1], jerk)


def extract_features(samples):
    """Compute a stable feature vector from rows of six raw IMU axes."""
    rows = [tuple(row[:6]) for row in samples if len(row) >= 6]
    if not rows:
        raise ValueError("a feature window must contain at least one IMU sample")

    accel_mags = []
    gyro_mags = []
    for ax, ay, az, gx, gy, gz in rows:
        accel_mags.append(math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G)
        gyro_mags.append(math.sqrt(gx * gx + gy * gy + gz * gz) / LSB_PER_DPS)

    accel_mean, accel_std = _mean_std(accel_mags)
    gyro_mean, gyro_std = _mean_std(gyro_mags)
    axis_stds = [_mean_std([row[i] for row in rows])[1] for i in range(6)]
    base = (
        accel_mean,
        accel_std,
        min(accel_mags),
        max(accel_mags),
        gyro_mean,
        gyro_std,
    )
    scaled = tuple(axis_stds[i] / (LSB_PER_G if i < 3 else LSB_PER_DPS) for i in range(6))
    return base + scaled + _rhythm(rows, accel_mags, accel_mean, accel_std)
