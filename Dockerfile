# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Set environment variables
# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: transcodes agent-recorded voice notes to the ogg/opus container
# WhatsApp's Cloud API actually accepts -- browsers can only record
# audio/webm;codecs=opus (Chrome) or audio/ogg;codecs=opus (Firefox) via
# MediaRecorder, and WhatsApp silently rejects the former.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirement.txt .

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requirement.txt

# Copy project files
COPY . .

# Expose port that FastAPI runs on
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the application with uvicorn
CMD ["uvicorn", "chatbot.main:app", "--host", "0.0.0.0", "--port", "8000"]
