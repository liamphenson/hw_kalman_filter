"""Directed, protocol, and seeded random verification of the scalar filter."""

import os
import random
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from kalman_model import KalmanModel

WIDTH = int(os.environ["TEST_DATA_WIDTH"])
FRAC = int(os.environ["TEST_FRAC_BITS"])
MAXIMUM = (1 << WIDTH) - 1
SEED = int(os.environ["TEST_SEED"])


class FilterDriver:
    """Drive at falling edges; sample registered responses after HDL settles."""

    def __init__(self, dut):
        self.dut = dut
        self.model = KalmanModel(WIDTH, FRAC)

    async def reset(self):
        d = self.dut
        await FallingEdge(d.clk)
        d.reset.value = 1
        d.measurement_valid.value = 0
        d.wrQ.value = 0
        d.wrR.value = 0
        d.Q.value = 0
        d.R.value = 0
        d.measurement_in.value = 0
        for _ in range(2):
            await RisingEdge(d.clk)
            await ReadOnly()
            assert int(d.measurement_out.value) == 0
            assert int(d.P_k.value) == 1
            assert int(d.output_valid.value) == 0
            assert int(d.measurement_ready.value) == 0
        await FallingEdge(d.clk)
        d.reset.value = 0
        self.model = KalmanModel(WIDTH, FRAC)

    async def configure(self, q=None, r=None):
        d = self.dut
        await FallingEdge(d.clk)
        d.Q.value = q if q is not None else MAXIMUM
        d.R.value = r if r is not None else MAXIMUM
        d.wrQ.value, d.wrR.value = int(q is not None), int(r is not None)
        await RisingEdge(d.clk)
        await ReadOnly()
        await FallingEdge(d.clk)
        d.wrQ.value, d.wrR.value = 0, 0
        if q is not None:
            self.model.q = q
        if r is not None:
            self.model.r = r

    def check_result(self, expected):
        actual = int(self.dut.measurement_out.value), int(self.dut.P_k.value)
        assert actual == expected, f"estimate/covariance: got {actual}, expected {expected}"

    async def sample(self, measurement, *, same_edge_config=None, busy_config=None):
        d = self.dut
        await FallingEdge(d.clk)
        assert int(d.measurement_ready.value), "driver attempted a sample while busy"
        d.measurement_in.value = measurement
        d.measurement_valid.value = 1
        if same_edge_config is not None:
            q, r = same_edge_config
            d.Q.value, d.R.value = q, r
            d.wrQ.value, d.wrR.value = 1, 1
            self.model.q, self.model.r = q, r
        expected = self.model.step(measurement)
        old_output = int(d.measurement_out.value)
        await RisingEdge(d.clk)
        await ReadOnly()
        assert not int(d.measurement_ready.value)
        assert not int(d.output_valid.value)
        await FallingEdge(d.clk)
        d.measurement_valid.value = 0
        # Deliberately change the input while the original sample is in flight.
        d.measurement_in.value = MAXIMUM - measurement
        d.wrQ.value, d.wrR.value = 0, 0
        if busy_config is not None:
            d.Q.value, d.R.value = busy_config
            d.wrQ.value, d.wrR.value = 1, 1
        for cycle in range(FRAC + 10):
            await RisingEdge(d.clk)
            await ReadOnly()
            if int(d.output_valid.value):
                self.check_result(expected)
                assert int(d.measurement_ready.value)
                # Normal updates take FRAC+4 clocks after acceptance; R=0
                # bypasses the divider and takes two clocks.
                assert cycle + 1 == (2 if self.model.r == 0 else FRAC + 4)
                if busy_config is not None:
                    self.model.q, self.model.r = busy_config
                return expected
            assert int(d.measurement_out.value) == old_output, "output changed without output_valid"
            await FallingEdge(d.clk)
            d.wrQ.value, d.wrR.value = 0, 0
        raise AssertionError("filter did not respond within the bounded timeout")


async def setup(dut):
    dut.clk.value = 0
    dut.reset.value = 1
    dut.measurement_valid.value = 0
    dut.wrQ.value = 0
    dut.wrR.value = 0
    dut.Q.value = 0
    dut.R.value = 0
    dut.measurement_in.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    driver = FilterDriver(dut)
    await driver.reset()
    return driver


@cocotb.test()
async def known_half_gain_and_idle(dut):
    """Hand-computed gain=1/2 sequence includes a negative half-LSB correction."""
    driver = await setup(dut)
    await driver.configure(q=1, r=2)
    for measurement, expected_x in [(10, 5), (0, 2), (10, 6)]:
        x, p = await driver.sample(measurement)
        assert (x, p) == (expected_x, 1)
    # No valid request means no unsolicited filtering or repeated results.
    for _ in range(FRAC + 6):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert not int(dut.output_valid.value)
        assert int(dut.measurement_out.value) == 6
        assert int(dut.P_k.value) == 1


@cocotb.test()
async def zero_noise_and_full_range(dut):
    driver = await setup(dut)
    # R=0 trusts measurements, even when P-=0 (the explicit 0/0 policy).
    await driver.configure(q=0, r=0)
    for z in [MAXIMUM, 0, MAXIMUM // 2]:
        assert await driver.sample(z) == (z, 0)
    # With P-=0 and positive R, the estimate has zero gain and holds.
    await driver.configure(q=0, r=MAXIMUM)
    previous = driver.model.x
    assert await driver.sample(MAXIMUM) == (previous, 0)
    # Exercise P+Q and P+Q+R above the input word's range.
    await driver.configure(q=MAXIMUM, r=MAXIMUM)
    for z in [0, MAXIMUM, MAXIMUM, 0] * 4:
        await driver.sample(z)


@cocotb.test()
async def configuration_is_sampled_at_acceptance(dut):
    driver = await setup(dut)
    # Same-edge writes affect this sample; busy writes affect the NEXT sample.
    await driver.sample(MAXIMUM, same_edge_config=(4, 7), busy_config=(1, 0))
    assert await driver.sample(3) == (3, 0)
    # An unstrobed change of the configuration data buses must have no effect.
    await FallingEdge(dut.clk)
    dut.Q.value, dut.R.value = MAXIMUM, MAXIMUM
    for _ in range(2):
        await RisingEdge(dut.clk)
        await ReadOnly()
    assert await driver.sample(7) == (7, 0)
    await driver.configure(q=4, r=5)
    await driver.configure(q=7)  # R must remain 5 despite the unstrobed R bus.
    await driver.sample(MAXIMUM)
    await driver.configure(r=11)  # Q must remain 7.
    await driver.sample(0)


@cocotb.test()
async def reset_aborts_inflight_sample(dut):
    driver = await setup(dut)
    await FallingEdge(dut.clk)
    dut.measurement_in.value = MAXIMUM
    dut.measurement_valid.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert not int(dut.measurement_ready.value)
    for _ in range(max(2, FRAC // 2)):
        await RisingEdge(dut.clk)
        await ReadOnly()
    # Reset both the filter and its child divider before a result is committed.
    await driver.reset()
    for _ in range(FRAC + 6):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert not int(dut.output_valid.value), "aborted transaction leaked a response"
        assert int(dut.measurement_out.value) == 0
    await driver.sample(9)


@cocotb.test()
async def held_valid_and_back_to_back_transactions(dut):
    driver = await setup(dut)
    await driver.configure(q=2, r=3)
    pending = deque()
    accepted = completed = 0
    target = 24
    for _ in range(target * (FRAC + 10)):
        await FallingEdge(dut.clk)
        # Leave valid asserted throughout busy periods. A transaction occurs
        # only when ready was also high at its sampling edge.
        dut.measurement_valid.value = int(accepted < target)
        measurement = (accepted * 7) & MAXIMUM
        dut.measurement_in.value = measurement
        if accepted < target and int(dut.measurement_ready.value):
            pending.append(driver.model.step(measurement))
            accepted += 1
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.output_valid.value):
            assert pending, "response without an accepted request"
            driver.check_result(pending.popleft())
            completed += 1
        if completed == target:
            break
    assert accepted == completed == target
    assert not pending


@cocotb.test()
async def randomized_integer_scoreboard(dut):
    driver = await setup(dut)
    rng = random.Random(SEED)
    dut._log.info("Random seed=%d, width=%d, gain fraction=%d", SEED, WIDTH, FRAC)
    for index in range(300):
        if index % 9 == 0:
            await driver.configure(rng.randrange(MAXIMUM + 1), rng.randrange(MAXIMUM + 1))
        previous = driver.model.x
        measurement = rng.randrange(MAXIMUM + 1)
        actual, _ = await driver.sample(measurement)
        assert min(previous, measurement) <= actual <= max(previous, measurement)
