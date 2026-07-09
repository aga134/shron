FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --uid 1000 --user-group --no-create-home app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY . .

USER app

CMD ["python", "main.py"]
