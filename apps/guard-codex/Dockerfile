FROM python:3.14-slim

# Security: Create non-root user first
RUN groupadd --gid 1000 gitguard && \
    useradd --uid 1000 --gid gitguard --shell /bin/bash --create-home gitguard

# Install system dependencies with security updates
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
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

# Copy application code and set ownership
COPY --chown=gitguard:gitguard apps/guard-codex/ /app/

# Security: Switch to non-root user
USER gitguard

# Environment configuration
ENV PYTHONUNBUFFERED=1

# Security: Run with explicit user
CMD ["python","-m","asyncio","worker.py"]

# If you want both the worker and the NATS trigger in one container, replace CMD with a small supervisor or run two replicas (cleaner).
