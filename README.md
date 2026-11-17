# Scalar Kalman filter in SystemVerilog

For quick RTL checks before simulation, run `python3 tools/rtl_lint.py` from this
directory. Add `--synth-check` for generic structural checks. See the
[RTL lint guide](docs/RTL_LINT.md) for checking other modules, configuration,
diagnostics, JSON reports, and automation.

This project implements the scalar random-walk filter in the data-flow drawing:

```text
x_pred = x
p_pred = p + Q
gain   = p_pred / (p_pred + R)
x_next = x_pred + gain * (measurement - x_pred)
p_next = p_pred - gain * p_pred
```

The model has F=H=1. It estimates one unsigned sensor quantity. It does not yet
integrate acceleration, estimate bias, fuse correlated observations, or implement
CAN. Compile both files in `srcs/` when instantiating `kalman`.

## Numeric contract

- `DATA_WIDTH` defaults to 16. Measurements, estimates, Q, R, and the stored
  covariance are **unsigned integers**, with range 0 through 2^DATA_WIDTH-1.
  Negative physical values need an agreed offset encoding or a future signed
  interface; negative innovations are already supported internally.
- Q and R are **variances**, in squared sensor-count units, not standard
  deviations. Q is the process variance for one prediction interval.
- `GAIN_FRAC_BITS` defaults to `DATA_WIDTH`. Only the gain and intermediate
  correction products retain fractional precision. There is no floating point.
- The gain is represented as an unsigned integer G with scale L=2^GAIN_FRAC_BITS.
  Normal division returns `G = floor(p_pred * L / (p_pred + R))`. There is one
  extra gain bit to represent exactly 1 when G=L.
- The state correction magnitude is `floor((G * abs(z-x) + L/2) / L)`.
  Apply its sign afterward. This rounds to nearest, with half-count corrections
  away from zero. For example, x=5, z=0, K=1/2 produces x_next=2.
- Posterior covariance is `floor((p_pred * (L-G) + L/2) / L)`, then saturated
  to the storage maximum. The RTL subtracts the full-precision product before
  rounding; rounding the subtracted product first would change tie cases.
- Prediction uses DATA_WIDTH+1 bits, and the denominator DATA_WIDTH+2 bits.
  Products retain their full precision before rounding.
- DATA_WIDTH and GAIN_FRAC_BITS must both be at least 1. The supplied regression
  requires DATA_WIDTH >= 4 for its hand-computed test vectors.

Integer sensor readings do not imply that the underlying estimate or variance
is an integer. This intentionally simple version can stall when a correction
rounds to zero, and covariance quantization can substantially change the filter.
The tests establish agreement with this integer contract; they do not establish
optimal estimation performance. A later version can retain fractional state and
covariance while keeping the sensor ports integer-valued.

## Clock, reset, and interface

All sequential logic uses the rising clock edge and an active-high **synchronous**
reset. Reset cancels any in-flight transaction, clears output_valid, and restores
Q=R=1, x=INITIAL_ESTIMATE, p=INITIAL_COVARIANCE. The initialization parameters
default to x=0 and p=1. Assert reset across at least one rising edge before use.

| Signal | Meaning |
| --- | --- |
| measurement_in | Unsigned input sample |
| measurement_valid | Producer has a sample available |
| measurement_ready | Filter can accept a sample |
| measurement_out | Most recently committed estimate; holds between responses |
| output_valid | Committed estimate is new on this cycle |
| wrQ / Q | Write the process variance at a rising edge |
| wrR / R | Write the measurement variance at a rising edge |

A sample is accepted at a rising edge with both measurement_valid and
measurement_ready high. A producer must hold its pending valid/data until
acceptance. There is no input queue. One accepted sample produces one response,
unless reset aborts it. There is no output backpressure: the consumer must observe
output_valid and capture the result. A continuously asserted input valid sends a
new sample each time ready returns; it does not mean a one-time trigger.

Configuration writes work while idle or busy. The filter snapshots configuration
when accepting a sample. Same-edge writes apply to that accepted sample; later
writes affect subsequent samples. Changes on Q/R without their write strobes have
no effect. Both writes can occur on the same edge, independently of each other.

R=0 is an explicit special policy: trust the measurement exactly and set posterior
covariance to zero. This includes p_pred=R=0, whose formal gain would be undefined.
For p_pred=0 and R>0, gain is zero and the estimate holds.

## Datapath and latency

The filter controller follows:

```text
IDLE -> DIV_START -> DIV_WAIT -> MULTIPLY -> COMMIT -> IDLE
              child divider runs here
```

Acceptance in IDLE captures the predicted covariance, denominator, and residual
sign/magnitude. State and covariance remain unchanged while busy. The divider
computes one quotient bit per rising edge. MULTIPLY registers the two correction
products concurrently. COMMIT rounds and atomically updates state/covariance.

For R>0, output_valid asserts **GAIN_FRAC_BITS+4 clock periods after acceptance**.
With the defaults this is 20 periods. The next sample can be accepted on the
following rising edge, giving a minimum initiation interval of 21 periods. R=0
bypasses the divider and returns after two periods. These cycle counts are checked
by the testbench, but they are implementation choices rather than the essence of
the valid/ready protocol. Changing the pipeline requires updating the latency
checks. Timing closure at a chosen FPGA clock still needs synthesis/place/route.

`fractional_divider` is independently reusable:

- Parameters WIDTH and FRAC_BITS specify unsigned operand width and gain precision.
- Its input_valid/input_ready handshake captures numerator and denominator.
- For numerator < denominator, it returns the truncated ratio after FRAC_BITS
  periods. The invariant is `0 <= remainder < denominator`.
- Each iteration doubles the remainder, compares/subtracts the divisor, and
  shifts the resulting quotient bit into the accumulator.
- A numerator >= nonzero denominator saturates the result to 1. A zero
  denominator returns zero with divide_by_zero=1. Both endpoint responses are
  registered on the acceptance edge. They do not occur in the normal filter path.
- Quotient has FRAC_BITS+1 bits. Divide_by_zero is meaningful with output_valid.
- A request asserted while input_ready=0 is not accepted. Reset cancels work.

There are no `/` or `*` operators in the divider. The filter contains two `*`
operators. Actual DSP usage depends on operand widths and the target FPGA.

## Running the tests

The original `.venv` uses Intel Python 3.7 and Intel cocotb binaries, while the
installed OSS CAD Suite simulators are Apple Silicon binaries. Loading those
original cocotb libraries into native Icarus fails. The original venv is preserved;
the working environment created for this project is `.venv-native` with native
Python 3.11 and cocotb 2.0.1.

From the repository root:

```sh
.venv-native/bin/python tb/run.py
.venv-native/bin/python tb/run.py --module kalman
.venv-native/bin/python tb/run.py --module fractional_divider
.venv-native/bin/python tb/run.py --matrix
.venv-native/bin/python tb/run.py --sim verilator --matrix
.venv-native/bin/python tb/run.py --module kalman --test-filter known_half --waves
.venv-native/bin/python tb/run.py --data-width 12 --frac-bits 10 --seed 17
```

Alternatively, activate `.venv-native` and use `python tb/run.py`. A fresh native
environment on this machine can be created with:

```sh
/opt/homebrew/bin/python3.11 -m venv .venv-native
.venv-native/bin/python -m pip install -r tb/requirements.txt
```

On other machines choose a Python whose architecture matches the simulator.
Both simulators must be on PATH. Verilator also requires a working C++ compiler
and make; the runner explicitly selects C++17 for the generated simulation model.

The runner uses the cocotb Python runner API, so pytest and cocotb-test are not
required. Build products, per-run `results.xml`, and optional waves live under
`sim_build/<simulator>_<module>_w<width>_f<fraction>/`. The script checks the XML
results and exits nonzero on failure or if no tests ran. Preserve the reported
seed and width/precision when reproducing a failure.

The OSS CAD Suite vvp shell wrapper sets PYTHONHOME/PYTHONEXECUTABLE to its own
bundled Python. The runner detects this wrapper and creates a project-local
symlink to its underlying vvp executable, preserving the selected venv instead.
It does not edit your tool installation. Plain Icarus installations are unchanged.

## Walkthrough of this cocotb testbench

### 1. The runner builds and starts the HDL simulator

`tb/run.py` selects Icarus or Verilator with `get_runner`, lists the SystemVerilog
sources, supplies elaboration parameters, and calls `runner.build()`. Then
`runner.test()` starts the simulator and loads `test_kalman` or
`test_fractional_divider` into its embedded Python runtime. Parameters are selected
before simulation, rather than changed by driving signals.

The `--matrix` option elaborates both modules at (DATA_WIDTH, GAIN_FRAC_BITS)
values (16,16), (8,6), and (5,1). Testing a one-bit quotient fraction catches
counter and slice boundary mistakes that default-width testing can miss.

### 2. Decorated asynchronous functions are test cases

In `tb/test_kalman.py`:

```python
@cocotb.test()
async def known_half_gain_and_idle(dut):
    driver = await setup(dut)
    await driver.configure(q=1, r=2)
    x, p = await driver.sample(10)
    assert (x, p) == (5, 1)
```

`dut` is a handle to the elaborated top-level module. `dut.measurement_in.value`
reads or drives a signal. `int(signal.value)` converts a resolved bit vector to a
Python integer. An unresolved X/Z in a checked value causes a conversion failure
instead of silently becoming zero. A Python assertion failure fails the test.

`async`/`await` allows a test to suspend while simulation time or another event
progresses. It is not a software delay or a separate HDL clock.

### 3. Start the clock and reset the DUT

`setup()` initializes every input and starts the clock concurrently:

```python
cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
```

It then asserts synchronous reset across rising edges. Cocotb cleans up the clock
task when the test ends. Each decorated test performs its own reset so state does
not leak from one test into another.

### 4. Drive and sample in different simulation phases

The driver changes inputs at falling edges:

```python
await FallingEdge(dut.clk)
dut.measurement_in.value = measurement
dut.measurement_valid.value = 1
await RisingEdge(dut.clk)
await ReadOnly()
```

The falling edge gives inputs half a cycle to settle before the sampling edge.
RisingEdge alone does not guarantee that the RTL's nonblocking assignments have
completed. ReadOnly waits until signals have settled before inspecting them.
Do not drive signals from that read-only phase; wait for a later falling edge.
This discipline works with both event-driven Icarus and cycle-based Verilator.

### 5. Separate the driver from the mathematical model

`FilterDriver.configure()` writes Q/R. `sample()` accepts one transaction, waits
with a bounded timeout, checks that the output holds while busy, and checks the
response. It deliberately changes the measurement bus after acceptance to detect
missing input latches. It also checks ready/valid and the documented latency.

`KalmanModel.step()` in `tb/kalman_model.py` is the numerical predictor. It uses
Python's unlimited-precision integers and direct integer division, rather than
copying the RTL's iterative divider. It explicitly applies the specified gain
quantization, rounding, and saturation. The expected x/p pair is the scoreboard
entry for one accepted sample.

The scoreboard compares both the output port and internal `P_k`. The covariance
check is deliberately a small white-box check; it must be renamed if that register
is renamed. A future externally exposed covariance/status interface could replace
this hierarchical access.

### 6. Exercise arithmetic and protocol behavior

The six filter tests cover:

- A hand-computed half-gain sequence, both residual signs, and idle stability.
- Zero noise, zero predicted covariance, maximum-width inputs, and wide sums.
- Same-edge configuration, configuration written while busy, and independent
  Q/R write strobes.
- Reset during an in-flight update and recovery without a stale response.
- Continuously asserted valid, consecutive transactions, and response accounting.
- 300 reproducible random samples with changing Q/R and a bounded-estimate check.

The three divider tests cover endpoints and all ratios with denominators 1..16,
300 random full-width ratios, input capture, latency, busy request rejection, and
reset/recovery. The divider's oracle is Python integer division. Directed
hand-computed filter results provide an additional check on the numerical model.

There is no functional coverage collector yet. The random tests supplement the
directed cases; they do not prove exhaustive state-space or analog-noise coverage.

## Creating the next module testbench

1. Write a contract: reset, legal input values, acceptance edge, response timing,
   backpressure, configuration semantics, overflow, and exceptional cases.
2. Add the module and its source dependencies to `tb/run.py`. Supply parameters
   during `runner.build()` and make their values available to the Python model.
3. Add `tb/test_<module>.py` with a reset helper and the smallest hand-checkable
   directed case. Start the clock, drive away from the sampling edge, and read
   after ReadOnly. Bound every wait for a response.
4. Build a transaction-level reference model independent of the RTL algorithm.
   Match the intended bit widths and rounding rather than comparing fixed point
   RTL directly to unrestricted floating point results.
5. Add boundary values, idle/busy behavior, reset during work, and configuration
   timing. Add seeded random sequences only after the directed cases work.
6. When more than one request can be outstanding, keep an expected-results queue
   and use a separate monitor to compare outputs in order. The held-valid test
   demonstrates acceptance accounting with a small `deque` scoreboard.
7. Reproduce failures with their seed/parameters, enable waves, and inspect the
   first mismatch. Add a directed regression case for the underlying bug.
8. Run the suite automatically with a script or CI and preserve XML results.
   Add meaningful coverage targets as the design grows.

## Preparing for UVM later

The current suite is cocotb, not SystemVerilog UVM. UVM is a separate verification
methodology and class library standardized by IEEE 1800.2. Its familiar pieces map
to this testbench as follows:

| Current concept | Future UVM concept |
| --- | --- |
| Measurement/configuration operation | Sequence item (transaction) |
| Directed or randomized sample stream | Sequence and sequencer |
| FilterDriver signal-level methods | Driver |
| Observing accepted inputs and valid outputs | Monitor |
| KalmanModel plus expected-result comparisons | Predictor and scoreboard |
| Arithmetic/protocol scenario targets | Functional coverage |

For full-system verification, build reusable agents around the actual interfaces
(CAN frames, register accesses, sensor transactions), then connect their monitors
to a system predictor and scoreboard. Keep the mathematical model separate from
bus timing so its vectors/specification remain reusable. A UVM regression also
needs simulator support for the chosen SystemVerilog/UVM implementation; check
that separately from whether a simulator supports this synthesizable RTL. The
Python suite does not depend on installing or running UVM.

## References

- [cocotb 2.0.1 documentation](https://docs.cocotb.org/en/v2.0.1/)
- [Python build/test runner](https://docs.cocotb.org/en/v2.0.0/runner.html)
- [Simulation timing model](https://docs.cocotb.org/en/v2.0.0/timing_model.html)
- [Accellera IEEE 1800.2 reference library](https://github.com/accellera-official/uvm-core)
# hw_kalman_filter
