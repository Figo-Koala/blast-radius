FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    SKIP_INTEGRATION=0 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.lock

# Fetch the competition's immutable SQLGlot source snapshot.
RUN python -c 'import urllib.request; url="https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa"; urllib.request.urlretrieve(url, "/tmp/sqlglot.tar.gz")' \
    && mkdir -p /opt/sqlglot \
    && tar -xzf /tmp/sqlglot.tar.gz -C /opt/sqlglot --strip-components=1 \
    && rm /tmp/sqlglot.tar.gz \
    && python -m compileall -q -j 4 /opt/sqlglot/sqlglot /opt/sqlglot/tests

COPY selector.py impact.py patches.py schedule.py scalars.py parser_guards.py verify.py dependencies.json.gz ./

ENV HOME=/tmp
USER 65534:65534
ENTRYPOINT ["python", "/app/selector.py"]
