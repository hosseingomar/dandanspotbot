FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=7860

# Install ffmpeg, system packages and Deno (JS runtime required by yt-dlp EJS
# challenge solver for signature/n challenges). Minimum Deno 2.3.0.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    gcc \
    libpq-dev \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "arm64" ]; then DENO_ARCH="aarch64-unknown-linux-gnu"; else DENO_ARCH="x86_64-unknown-linux-gnu"; fi \
    && curl -fsSL "https://dl.deno.land/release/v2.6.4/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version && ffmpeg -version | head -n 1

# Set up user 1000 (standard for Hugging Face Spaces)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Install dependencies
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy source code
COPY --chown=user:user . .

# Ensure downloads directory exists
RUN mkdir -p $HOME/app/downloads

# Expose port (Hugging Face default is 7860)
EXPOSE 7860

CMD ["python", "main.py"]
