FROM python:3.12-slim

# e2fsprogs provides e2fsck / resize2fs / dumpe2fs / debugfs.  No mount or
# loop device is ever used: the images are resized while unmounted.
RUN apt-get update \
 && apt-get install -y --no-install-recommends e2fsprogs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY seed ./seed
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd --system --uid 10001 --home-dir /srv app \
 && mkdir -p /data /state \
 && chown -R app:app /srv /data /state \
 && chmod +x /usr/local/bin/entrypoint.sh /srv/scripts/*.sh \
 && python3 -m py_compile /srv/app/main.py

USER app
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
