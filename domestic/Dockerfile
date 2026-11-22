FROM python:3.12-slim
WORKDIR /opt/sucang
COPY domestic/requirements.txt domestic/
RUN pip install --no-cache-dir -r domestic/requirements.txt && useradd --system --uid 10001 app && mkdir -p /var/lib/sucang && chown app /var/lib/sucang
COPY domestic/app.py domestic/
COPY dist/index.html dist/index.html
ENV DATA_DIR=/var/lib/sucang COOKIE_SECURE=1
USER app
WORKDIR /opt/sucang/domestic
EXPOSE 8080
CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "app:app"]
