FROM python:3.14-slim

# Security: Create non-root user first
RUN groupadd --gid 1000 gitguard && \
    useradd --uid 1000 --gid gitguard --shell /bin/bash --create-home gitguard

# Install system dependencies with security updates
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Copy requirements and install Python dependencies
COPY apps/guard-codex/requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip cache purge

# Copy only the nats_runner.py file
COPY --chown=gitguard:gitguard apps/guard-codex/nats_runner.py /app/

# Security: Switch to non-root user
USER gitguard

# Environment configuration
ENV PYTHONUNBUFFERED=1

# Run the NATS trigger
CMD ["python", "nats_runner.py"]
