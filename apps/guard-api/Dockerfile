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
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip cache purge

# Copy application code and set ownership
COPY --chown=gitguard:gitguard . .

# Security: Switch to non-root user
USER gitguard

# Security: Use non-privileged ports
EXPOSE 8000 8080

# Security: Run with explicit user and read-only filesystem where possible
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
