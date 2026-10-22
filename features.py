"""Timestamp-agnostic, MicroPython-friendly features for fixed sample windows."""
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
    # Orientation-independent totals: sqrt of the summed per-axis variances.
    "gyro_axis_std",
    "accel_axis_std",
)
AXIS_NAMES = ("ax", "ay", "az", "gx", "gy", "gz")
LSB_PER_G = 16384.0
LSB_PER_DPS = 70.0
# Below this acceleration magnitude the badge is treated as airborne.
FREEFALL_G = 0.4


def _mean_std(values):
    n = len(values)
    if not n:
        return 0.0, 0.0
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    return mean, math.sqrt(variance)


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
    axis_stds = [_mean_std([row[i] for row in rows])[1] / (LSB_PER_G if i < 3 else LSB_PER_DPS) for i in range(6)]
    return (
        accel_mean,
        accel_std,
        min(accel_mags),
        max(accel_mags),
        gyro_mean,
        gyro_std,
        *axis_stds,
        math.sqrt(sum(value * value for value in axis_stds[3:])),
        math.sqrt(sum(value * value for value in axis_stds[:3])),
    )
