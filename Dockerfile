FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . ./

RUN chmod +x /app/docker-entrypoint.sh

RUN mkdir -p /app/lib /app/data /app/web/static
VOLUME ["/app/data", "/app/lib", "/app/web/static"]

EXPOSE 1080 5003

ENTRYPOINT ["/app/docker-entrypoint.sh"]
