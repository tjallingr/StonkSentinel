FROM python:3.12-slim-bookworm

# arm64 wheels exist for everything in requirements.txt, so no compiler needed
# on the Pi. If a future dep lacks an aarch64 wheel this is where it'll hurt.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1

RUN useradd --system --uid 10001 finoverview && mkdir -p /app/data
USER 10001

EXPOSE 8080
CMD ["uvicorn", "finoverview.web.app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--no-access-log"]
