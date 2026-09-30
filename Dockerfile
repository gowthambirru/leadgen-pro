FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency definition
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy webapp source code
COPY . .

# Create volume directories for SQLite persistence and downloads
RUN mkdir -p data downloads

EXPOSE 8000

ENV HOST=0.0.0.0
ENV PORT=8000

CMD ["python", "run.py"]
