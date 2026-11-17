"""Check the divider against Python integer division, including its protocol."""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

WIDTH = int(os.environ["TEST_DATA_WIDTH"]) + 2
FRAC = int(os.environ["TEST_FRAC_BITS"])
MAXIMUM = (1 << WIDTH) - 1
SCALE = 1 << FRAC


async def reset(dut):
    await FallingEdge(dut.clk)
    dut.reset.value = 1
    dut.input_valid.value = 0
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert not int(dut.output_valid.value)
    assert not int(dut.input_ready.value)
    assert int(dut.quotient.value) == 0
    await FallingEdge(dut.clk)
    dut.reset.value = 0


async def setup(dut):
    dut.clk.value = 0
    dut.reset.value = 1
    dut.input_valid.value = 0
    dut.numerator.value = 0
    dut.denominator.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)


async def divide(dut, numerator, denominator):
    await FallingEdge(dut.clk)
    assert int(dut.input_ready.value)
    dut.numerator.value = numerator
    dut.denominator.value = denominator
    dut.input_valid.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    expected = 0 if denominator == 0 else min(SCALE, numerator * SCALE // denominator)
    immediate = denominator == 0 or numerator >= denominator
    if immediate:
        assert int(dut.output_valid.value)
        assert int(dut.quotient.value) == expected
        assert int(dut.divide_by_zero.value) == int(denominator == 0)
    else:
        assert not int(dut.output_valid.value)
        assert not int(dut.input_ready.value)
    await FallingEdge(dut.clk)
    dut.input_valid.value = 0
    # Inputs are deliberately corrupted after acceptance to check latching.
    dut.numerator.value = 0
    dut.denominator.value = 0
    if immediate:
        return
    old_quotient = int(dut.quotient.value)
    for cycle in range(1, FRAC + 2):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.output_valid.value):
            assert cycle == FRAC, f"wrong divider latency: {cycle}"
            assert int(dut.quotient.value) == expected, (numerator, denominator, expected)
            assert not int(dut.divide_by_zero.value)
            assert int(dut.input_ready.value)
            return
        assert int(dut.quotient.value) == old_quotient
    raise AssertionError("divider response timed out")


@cocotb.test()
async def endpoints_and_exhaustive_small_ratios(dut):
    await setup(dut)
    for n, d in [(0, 0), (1, 0), (0, 1), (1, 1), (MAXIMUM, 1),
                 (MAXIMUM - 1, MAXIMUM), (1, MAXIMUM), (MAXIMUM, MAXIMUM)]:
        await divide(dut, n, d)
    for denominator in range(1, 17):
        for numerator in range(denominator + 1):
            await divide(dut, numerator, denominator)


@cocotb.test()
async def randomized_full_width_ratios(dut):
    await setup(dut)
    rng = random.Random(int(os.environ["TEST_SEED"]))
    for _ in range(300):
        await divide(dut, rng.randrange(MAXIMUM + 1), rng.randrange(MAXIMUM + 1))


@cocotb.test()
async def reset_and_busy_request_rejection(dut):
    await setup(dut)
    await FallingEdge(dut.clk)
    dut.numerator.value = 1
    dut.denominator.value = 3
    dut.input_valid.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert not int(dut.input_ready.value)
    # A second asserted request while busy is not accepted. With FRAC=1 this
    # is also the result edge, so ready is checked BEFORE that rising edge.
    await FallingEdge(dut.clk)
    dut.numerator.value = MAXIMUM
    dut.denominator.value = 1
    assert not int(dut.input_ready.value)
    await RisingEdge(dut.clk)
    await ReadOnly()
    if FRAC == 1:
        assert int(dut.output_valid.value)
        assert int(dut.quotient.value) == SCALE // 3
    else:
        assert not int(dut.output_valid.value), "busy request was incorrectly accepted"
        assert not int(dut.input_ready.value)
    await FallingEdge(dut.clk)
    dut.input_valid.value = 0
    if FRAC > 1:
        # Still in flight; reset must cancel the calculation.
        await reset(dut)
    for _ in range(FRAC + 2):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert not int(dut.output_valid.value)
    await divide(dut, 1, 2)
