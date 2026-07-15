"""Runtime sanity checks. A failed check raises DensityError -> caller halts quoting."""
from __future__ import annotations
import numpy as np

from engine.density import SettleDensity


class DensityError(RuntimeError):
    pass


def sanity(d: SettleDensity) -> None:
    D = d._D
    if d.repair_max > 0.01 + 1e-12:
        raise DensityError(f"monotonicity repair {d.repair_max:.6f} exceeds 0.0100")
    if not (np.all(D >= -1e-9) and np.all(D <= 1 + 1e-9)):
        raise DensityError("survival outside [0,1]")
    if np.any(np.diff(D) > 1e-9):
        raise DensityError("survival not monotone")
    if D[0] < 0.995:
        raise DensityError(f"mass missing on left wing: D(K_min)={D[0]:.4f}")
    if D[-1] > 0.005:
        raise DensityError(f"mass missing on right wing: D(K_max)={D[-1]:.4f}")


def basis_bps(deribit_index: float, reference: float) -> float:
    """Deribit index vs another reference (e.g. Kalshi settle print), in bps."""
    return (deribit_index / reference - 1.0) * 1e4
