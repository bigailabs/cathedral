# Build stage
FROM rust:1.88-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    build-essential \
    protobuf-compiler \
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

# Build the autoscaler binary
RUN if [ "$BUILD_MODE" = "release" ]; then \
        cargo build --locked --release -p basilica-autoscaler; \
    else \
        cargo build --locked -p basilica-autoscaler; \
    fi && \
    rm -rf /tmp/target/debug/deps /tmp/target/debug/incremental /tmp/target/release/deps /tmp/target/release/incremental 2>/dev/null || true && \
    find /tmp/target -name "*.rlib" -delete 2>/dev/null || true

# Runtime stage
FROM debian:bookworm-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    ca-certificates \
    libssl3 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with explicit UID/GID 1000 to match K8s runAsUser.
# System ssh requires a valid /etc/passwd entry for the running user.
RUN groupadd -g 1000 basilica && useradd -u 1000 -g basilica -m basilica

# Copy the built binary
ARG BUILD_MODE=release
COPY --from=builder /tmp/target/${BUILD_MODE}/basilica-autoscaler /usr/local/bin/basilica-autoscaler

# Make binary executable
RUN chmod +x /usr/local/bin/basilica-autoscaler

# Switch to non-root user
USER basilica

# Expose ports
EXPOSE 8080 9090

# Default command
ENTRYPOINT ["/usr/local/bin/basilica-autoscaler"]
