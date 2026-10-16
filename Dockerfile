FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    TZ_NAME=Asia/Tashkent

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Все .py лежат в корне репозитория (так проще загружать с iPhone) — внутри
# контейнера они становятся пакетом «app».
COPY *.py ./app/

RUN mkdir -p /data
CMD ["python", "-m", "app"]
