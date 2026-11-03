# The hosted compute service (see docs/vercel-deployment.md and docs/supabase-architecture.md).
# Build from the repository root:   docker build -f deploy/engine/Dockerfile -t faqengine-hosted .
# NOT built or run in the repository's own checks (no Docker was available): treat the first build as part of deployment, and run the hosted tests (tests/test_hosted.py) against the image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY deploy/engine/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# only the parts the service imports; the training code (and PyTorch) stay out of the image
COPY src/faqengine/__init__.py /app/faqengine/__init__.py
COPY src/faqengine/kb.py src/faqengine/sources.py src/faqengine/web.py /app/faqengine/
COPY src/faqengine/runtime /app/faqengine/runtime
COPY src/faqengine/inference /app/faqengine/inference
COPY src/faqengine/hosted /app/faqengine/hosted
# the shared embedding model, built once with scripts/build_base_model.py
COPY hosted_models/minilm-l6 /models/minilm-l6

ENV EMBEDDING_MODEL_DIR=/models/minilm-l6/model PORT=8090
RUN useradd --system --no-create-home engine
USER engine
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8090'),timeout=4)"
CMD ["python", "-m", "faqengine.hosted.service"]
