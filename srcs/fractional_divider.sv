`timescale 1ns/1ps
`default_nettype none

// Unsigned ratio in [0,1], truncated to FRAC_BITS fractional bits.
// One shift/subtract iteration per clock, no multiplication or division operator.
// numerator >= denominator saturates to 1; denominator == 0 returns 0 + error.
// WIDTH >= 1 and FRAC_BITS >= 1 are required.
module fractional_divider #(
    parameter integer WIDTH = 18,
    parameter integer FRAC_BITS = 16
) (
    input logic clk,
    input logic reset,
    input logic input_valid,
    output logic input_ready,
    input logic [WIDTH-1:0] numerator,
    input logic [WIDTH-1:0] denominator,
    output logic [FRAC_BITS:0] quotient,
    output logic output_valid,
    output logic divide_by_zero
);
    localparam integer COUNT_WIDTH = (FRAC_BITS < 2) ? 1 : $clog2(FRAC_BITS+1);
    logic busy;
    logic [WIDTH-1:0] remainder_reg, divisor_reg;
    logic [FRAC_BITS-1:0] partial_quotient;
    logic [COUNT_WIDTH-1:0] bits_left;
    wire [WIDTH:0] trial = {remainder_reg, 1'b0};
    wire quotient_bit = trial >= {1'b0, divisor_reg};
    // The remainder invariant guarantees the result fits in WIDTH bits.
    wire [WIDTH-1:0] next_remainder = WIDTH'(quotient_bit
        ? trial - {1'b0, divisor_reg} : trial);
    wire [FRAC_BITS-1:0] next_quotient =
        (partial_quotient << 1) | FRAC_BITS'(quotient_bit);

    assign input_ready = !busy && !reset;

    always_ff @(posedge clk) begin
        if (reset) begin
            busy <= 1'b0;
            remainder_reg <= '0;
            divisor_reg <= '0;
            partial_quotient <= '0;
            bits_left <= '0;
            quotient <= '0;
            output_valid <= 1'b0;
            divide_by_zero <= 1'b0;
        end else begin
            output_valid <= 1'b0;
            if (busy) begin
                remainder_reg <= next_remainder;
                partial_quotient <= next_quotient;
                bits_left <= bits_left - 1'b1;
                if (bits_left == 1) begin
                    quotient <= {1'b0, next_quotient};
                    output_valid <= 1'b1;
                    busy <= 1'b0;
                end
            end else if (input_valid) begin
                divide_by_zero <= 1'b0;
                if (denominator == 0) begin
                    quotient <= '0;
                    divide_by_zero <= 1'b1;
                    output_valid <= 1'b1;
                end else if (numerator >= denominator) begin
                    quotient <= {1'b1, {FRAC_BITS{1'b0}}};
                    output_valid <= 1'b1;
                end else begin
                    remainder_reg <= numerator;
                    divisor_reg <= denominator;
                    partial_quotient <= '0;
                    bits_left <= COUNT_WIDTH'(FRAC_BITS);
                    busy <= 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
