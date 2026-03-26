# Stage 1: Builder
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies (e.g., poppler-utils for pdf2image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python packages from builder
COPY --from=builder /root/.local /root/.local

# Ensure the local bin is in PATH
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
