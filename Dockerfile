FROM python:3.13-alpine
COPY pihole-sync.py /app/pihole-sync.py
ENV PYTHONUNBUFFERED=1 \
    INTERVAL=300
USER nobody
ENTRYPOINT ["python3", "/app/pihole-sync.py"]
