FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY santa ./santa
RUN useradd -r -u 10001 appuser
USER appuser
CMD ["python", "-m", "santa"]
