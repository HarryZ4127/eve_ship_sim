"""Capacitor simulation.

Cap regenerates as dC/dt = 10*Cmax/tau * (sqrt(C/Cmax) - C/Cmax), whose closed form
over an interval is C(t) = Cmax * (1 + (sqrt(C0/Cmax) - 1) * exp(-5 t / tau))^2.
Each consumer drains its cost at the start of every cycle, all starting at t=0.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass


@dataclass
class CapUser:
    name: str
    amount: float     # GJ per cycle; negative for injectors (cap boosters)
    cycle_s: float    # time between activations
    charges: int = 0  # shots before reload (0 = unlimited)
    reload_s: float = 0.0


@dataclass
class CapResult:
    stable: bool
    lasts_s: float | None   # time until a module fails to activate (None if stable)
    stable_fraction: float  # mean of the lowest cap before and after an activation (0 if unstable)
    peak_recharge: float    # GJ/s at 25% cap
    use_per_s: float        # average drain of consumers, GJ/s (injectors excluded)
    inject_per_s: float     # average gain from injectors, GJ/s

    @property
    def delta_per_s(self) -> float:
        return self.peak_recharge + self.inject_per_s - self.use_per_s


def _recharge(c: float, cmax: float, tau: float, dt: float) -> float:
    if dt <= 0 or cmax <= 0:
        return c
    x = 1.0 + (math.sqrt(max(c, 0.0) / cmax) - 1.0) * math.exp(-5.0 * dt / tau)
    return cmax * x * x


def simulate(capacity: float, recharge_ms: float, users: list[CapUser], max_time_s: float = 3600.0) -> CapResult:
    tau = recharge_ms / 1000.0
    peak = 2.5 * capacity / tau if tau > 0 else 0.0
    users = [u for u in users if u.cycle_s > 0 and u.amount]
    use = 0.0
    inject = 0.0
    for u in users:
        period = u.cycle_s
        if u.charges:
            period = (u.charges * u.cycle_s + u.reload_s) / u.charges
        rate = u.amount / period
        if rate > 0:
            use += rate
        else:
            inject -= rate
    if not users or capacity <= 0:
        return CapResult(True, None, 1.0, peak, use, inject)
    if max_time_s <= 0:  # averages only: stable if peak recharge covers the net drain
        stable = peak + inject >= use
        return CapResult(stable, None, 1.0 if stable else 0.0, peak, use, inject)

    # Event queue of (time, idx, shots fired since reload)
    q = [(0.0, i, 0) for i in range(len(users))]
    heapq.heapify(q)
    c, t = capacity, 0.0
    low_post = low_pre = capacity
    while q:
        when, i, shots = heapq.heappop(q)
        if when > max_time_s:
            break
        c = _recharge(c, capacity, tau, when - t)
        t = when
        u = users[i]
        if u.amount > 0 and c < u.amount - 1e-9:
            return CapResult(False, t, 0.0, peak, use, inject)
        low_pre = min(low_pre, c)
        c = min(capacity, c - u.amount)
        low_post = min(low_post, c)
        shots += 1
        nxt = t + u.cycle_s
        if u.charges and shots >= u.charges:
            nxt += u.reload_s
            shots = 0
        heapq.heappush(q, (nxt, i, shots))
    return CapResult(True, None, (low_pre + low_post) / 2.0 / capacity, peak, use, inject)
