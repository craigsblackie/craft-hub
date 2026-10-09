FROM python:3.12-alpine
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx
WORKDIR /srv
COPY app /srv/app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
USER 99:100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
