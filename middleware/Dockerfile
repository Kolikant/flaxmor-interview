# Build context is the repository ROOT, not middleware/, so that SYSTEM_PROMPT.md can
# be copied in:
#   docker build -f middleware/Dockerfile .
# docker-compose.yml already sets context and dockerfile accordingly.
FROM python:3.11-slim

# 3.11 rather than a newer interpreter: it is the floor the assignment states, and
# running the stated minimum is the version most likely to reveal a compatibility
# problem a reviewer would hit.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Metadata and sources are copied together because the project is installed from
# pyproject.toml, so there is no second copy of the dependency list to drift from the
# real one. The consequence is that editing a source file invalidates this layer — the
# BuildKit cache mount is what stops that re-downloading all 34 packages every time,
# so PIP_NO_CACHE_DIR is deliberately *not* set.
COPY middleware/pyproject.toml /app/middleware/pyproject.toml
COPY middleware/src /app/middleware/src
RUN --mount=type=cache,target=/root/.cache/pip pip install /app/middleware

# The prompt document is the runtime source of truth rather than a Python constant.
# SYSTEM_PROMPT_PATH is set explicitly in compose so nothing depends on the loader's
# upward directory walk, which cannot find a repo-root file from inside site-packages.
COPY SYSTEM_PROMPT.md /app/SYSTEM_PROMPT.md

# Not named "proxy": Debian slim already ships a system user by that name, and
# useradd fails with exit 9 on the collision.
RUN useradd --create-home --uid 10001 extractor
USER extractor

EXPOSE 8000

# __main__ installs JSON logging before starting uvicorn; see extractor_proxy/__main__.py.
CMD ["python", "-m", "extractor_proxy"]
