FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Create user and the virtual environment directory structure
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser && \
    python -m venv $VIRTUAL_ENV && \
    chown -R appuser:appuser $VIRTUAL_ENV /app

COPY requirements.txt .

# Switch to the non-root user early so pip runs without root privileges
USER appuser

# Pip will now install directly into the virtual environment seamlessly
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
