# Crewboss — multi-stage: build the frontend (Tailwind CSS + the React Flow island via
# Vite) with node, then ship a plain python:3.12-slim runtime. No CDN at runtime: everything static
# (Tailwind CSS, htmx, Alpine, the workflow canvas bundle) is baked into the image at build time.

FROM node:20-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
COPY app/templates /build/app/templates
COPY app/*.py /build/app/
COPY app/static /build/app/static
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /app

# openssh-client: ssh-keygen (the per-account SSH CA signs passes) and ssh (Check access), task #367.
# git: read-only diffs in a linked admin site's mounted repo (no drift, #402).
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl openssh-client git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY db/ ./db/
COPY --from=frontend /build/app/static ./app/static

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "8192"]
