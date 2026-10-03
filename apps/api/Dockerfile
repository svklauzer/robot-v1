FROM python:3.11-slim

# Set environment variables to keep Python clean and responsive
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-root user and group for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

COPY requirements.txt .

# Upgrade pip and install packages directly as root (safe since it's an isolated container)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Change ownership of the application files to the non-root user
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

EXPOSE 8000

# Shell form so ${PORT} expands. Render injects PORT; locally defaults to 8000.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
