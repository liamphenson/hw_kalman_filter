"""Transaction-level integer model, independent of the RTL divider recurrence."""

from dataclasses import dataclass


@dataclass
class KalmanModel:
    width: int
    frac_bits: int
    x: int = 0
    p: int = 1
    q: int = 1
    r: int = 1

    def step(self, measurement):
        """One accepted sample; Python integers retain every intermediate bit."""
        scale = 1 << self.frac_bits
        predicted_p = self.p + self.q
        gain = scale if self.r == 0 else predicted_p * scale // (predicted_p + self.r)
        difference = measurement - self.x
        correction = (gain * abs(difference) + scale // 2) // scale
        self.x += -correction if difference < 0 else correction
        posterior_p = (predicted_p * (scale - gain) + scale // 2) // scale
        self.p = min((1 << self.width) - 1, posterior_p)
        return self.x, self.p
