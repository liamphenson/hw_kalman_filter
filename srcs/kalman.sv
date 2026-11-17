`timescale 1ns/1ps
`default_nettype none

module kalman #(
    parameter integer DATA_WIDTH = 16,
    parameter integer GAIN_FRAC_BITS = DATA_WIDTH,
    parameter logic [DATA_WIDTH-1:0] INITIAL_ESTIMATE = '0,
    parameter logic [DATA_WIDTH-1:0] INITIAL_COVARIANCE = 1
) (
    input  logic clk,
    input  logic reset,
    input  logic wrQ,
    input  logic [DATA_WIDTH-1:0] Q,
    input  logic wrR,
    input  logic [DATA_WIDTH-1:0] R,
    input  logic [DATA_WIDTH-1:0] measurement_in,
    input  logic measurement_valid,
    output logic measurement_ready,
    output logic [DATA_WIDTH-1:0] measurement_out,
    output logic output_valid
);
    localparam integer PRED_WIDTH = DATA_WIDTH + 1;
    localparam integer DEN_WIDTH = DATA_WIDTH + 2;
    localparam integer X_PRODUCT_WIDTH = DATA_WIDTH + GAIN_FRAC_BITS + 1;
    localparam integer P_PRODUCT_WIDTH = PRED_WIDTH + GAIN_FRAC_BITS + 1;
    localparam logic [GAIN_FRAC_BITS:0] GAIN_ONE = {1'b1, {GAIN_FRAC_BITS{1'b0}}};
    localparam logic [X_PRODUCT_WIDTH-1:0] X_HALF = {{(X_PRODUCT_WIDTH-1){1'b0}}, 1'b1} << (GAIN_FRAC_BITS-1);
    localparam logic [P_PRODUCT_WIDTH-1:0] P_HALF = {{(P_PRODUCT_WIDTH-1){1'b0}}, 1'b1} << (GAIN_FRAC_BITS-1);

    typedef enum logic [2:0] {IDLE, DIV_START, DIV_WAIT, MULTIPLY, COMMIT} state_t;
    state_t state;

    logic [DATA_WIDTH-1:0] Q_reg, R_reg;
    logic [DATA_WIDTH-1:0] x_k, P_k;
    logic [PRED_WIDTH-1:0] predicted_covariance;
    logic [DEN_WIDTH-1:0] denominator;
    logic [DATA_WIDTH-1:0] residual_magnitude;
    logic residual_negative;
    logic [GAIN_FRAC_BITS:0] gain;
    logic [X_PRODUCT_WIDTH-1:0] state_product;
    logic [P_PRODUCT_WIDTH-1:0] covariance_product;

    wire [DATA_WIDTH-1:0] sample_Q = wrQ ? Q : Q_reg;
    wire [DATA_WIDTH-1:0] sample_R = wrR ? R : R_reg;
    wire [PRED_WIDTH-1:0] next_prediction = {1'b0, P_k} + {1'b0, sample_Q};
    wire [DEN_WIDTH-1:0] next_denominator =
        {1'b0, next_prediction} + {{2{1'b0}}, sample_R};

    logic divider_valid, divider_ready;
    logic unused_divide_by_zero; // R>0 implies denominator>0; R=0 bypasses.
    logic [GAIN_FRAC_BITS:0] divider_quotient;
    fractional_divider #(
        .WIDTH(DEN_WIDTH), .FRAC_BITS(GAIN_FRAC_BITS)
    ) gain_divider (
        .clk(clk), .reset(reset),
        .input_valid(state == DIV_START), .input_ready(divider_ready),
        .numerator({1'b0, predicted_covariance}),
        .denominator(denominator),
        .quotient(divider_quotient), .output_valid(divider_valid),
        .divide_by_zero(unused_divide_by_zero)
    );

    wire [DATA_WIDTH-1:0] rounded_correction =
        DATA_WIDTH'((state_product + X_HALF) >> GAIN_FRAC_BITS);
    // Subtract at full fractional precision BEFORE rounding the posterior.
    wire [P_PRODUCT_WIDTH-1:0] scaled_prediction =
        {{(P_PRODUCT_WIDTH-PRED_WIDTH){1'b0}}, predicted_covariance}
        << GAIN_FRAC_BITS;
    wire [P_PRODUCT_WIDTH-1:0] rounded_covariance =
        (scaled_prediction - covariance_product + P_HALF) >> GAIN_FRAC_BITS;

    assign measurement_ready = (state == IDLE) && !reset;
    assign measurement_out = x_k;

    always_ff @(posedge clk) begin
        if (reset) begin
            state <= IDLE;
            Q_reg <= 1;
            R_reg <= 1;
            x_k <= INITIAL_ESTIMATE;
            P_k <= INITIAL_COVARIANCE;
            predicted_covariance <= '0;
            denominator <= '0;
            residual_magnitude <= '0;
            residual_negative <= 1'b0;
            gain <= '0;
            state_product <= '0;
            covariance_product <= '0;
            output_valid <= 1'b0;
        end else begin
            output_valid <= 1'b0;
            // Writes are allowed while busy, but cannot alter an in-flight
            // sample. Same-edge writes DO apply to a newly accepted sample.
            if (wrQ) Q_reg <= Q;
            if (wrR) R_reg <= R;

            case (state)
                IDLE: if (measurement_valid) begin
                    predicted_covariance <= next_prediction;
                    denominator <= next_denominator;
                    residual_negative <= measurement_in < x_k;
                    residual_magnitude <= (measurement_in < x_k)
                        ? x_k - measurement_in : measurement_in - x_k;
                    if (sample_R == 0) begin
                        // An explicitly noiseless measurement is authoritative,
                        // including the otherwise undefined P-=R=0 case.
                        gain <= GAIN_ONE;
                        state <= MULTIPLY;
                    end else begin
                        state <= DIV_START;
                    end
                end
                DIV_START: if (divider_ready) state <= DIV_WAIT;
                DIV_WAIT: if (divider_valid) begin
                    gain <= divider_quotient;
                    state <= MULTIPLY;
                end
                MULTIPLY: begin
                    // Two multiplication operators; neither is in the divider.
                    state_product <= gain * residual_magnitude;
                    covariance_product <= gain * predicted_covariance;
                    state <= COMMIT;
                end
                COMMIT: begin
                    // Rounded correction <= residual magnitude, so the new
                    // unsigned estimate stays between x_k and the measurement.
                    x_k <= residual_negative
                        ? x_k - rounded_correction
                        : x_k + rounded_correction;
                    // Saturation prevents wraparound at the storage boundary.
                    if (|rounded_covariance[P_PRODUCT_WIDTH-1:DATA_WIDTH])
                        P_k <= '1;
                    else
                        P_k <= rounded_covariance[DATA_WIDTH-1:0];
                    output_valid <= 1'b1;
                    state <= IDLE;
                end
                default: state <= IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
