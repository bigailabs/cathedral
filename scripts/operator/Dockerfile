# Build stage
FROM rust:1.88-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    build-essential \
    protobuf-compiler \
    libfuse3-dev \
    fuse3 \
    && rm -rf /var/lib/apt/lists/*

# Install rustfmt (keep in sync with workspace expectations)
RUN rustup component add rustfmt

# Set working directory
WORKDIR /workspace

# Copy workspace files
COPY Cargo.toml Cargo.lock ./
COPY crates/ ./crates/

# Fetch dependencies
RUN cargo fetch

# Build arguments
ARG BUILD_MODE=release

# Space-saving environment
ENV CARGO_TARGET_DIR=/tmp/target
ENV CARGO_INCREMENTAL=0
ENV RUST_BACKTRACE=1

# Build the operator binary
RUN if [ "$BUILD_MODE" = "release" ]; then \
        cargo build --locked --release -p basilica-operator; \
    else \
        cargo build --locked -p basilica-operator; \
    fi && \
    rm -rf /tmp/target/debug/deps /tmp/target/debug/incremental /tmp/target/release/deps /tmp/target/release/incremental 2>/dev/null || true && \
    find /tmp/target -name "*.rlib" -delete 2>/dev/null || true

# Runtime stage
FROM debian:bookworm-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    ca-certificates \
    libssl3 \
    libfuse3-3 \
    fuse3 \
    && rm -rf /var/lib/apt/lists/*

# Copy the built binary
ARG BUILD_MODE=release
COPY --from=builder /tmp/target/${BUILD_MODE}/basilica-operator /usr/local/bin/basilica-operator

# Make binary executable
RUN chmod +x /usr/local/bin/basilica-operator

# Expose metrics port
EXPOSE 9400

# Default command
ENTRYPOINT ["/usr/local/bin/basilica-operator"]

