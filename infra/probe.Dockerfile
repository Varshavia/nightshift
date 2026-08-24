# The environment the probe measures in — deliberately the same shape as the
# worker's, because a measurement taken somewhere else measures somewhere else.
#
#   docker build -f infra/probe.Dockerfile -t nightshift-probe .
#   docker run --rm -v "$PWD/fleet:/app/fleet" -v "$PWD/benchmark:/app/benchmark" \
#     nightshift-probe --out benchmark/cases.json
#
# The first probe run was done on Windows and produced six failures, and the
# same six repositories on Linux failed differently: one suite went from red to
# green purely by changing operating system. Python applications assume POSIX in
# ways their authors never wrote down, and Cloud Run is Linux, so the number we
# publish has to come from Linux.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/packages

WORKDIR /app

# Two groups, and the difference between them is the point.
#
# `git` and `ca-certificates` are ours: without them the probe cannot clone.
#
# Everything after is somebody else's build dependency — the C libraries that
# `mysqlclient`, `psycopg2`, `lxml` and `Pillow` compile against. They are here
# because of a measurement: PowerDNS-Admin, a real Flask application with 43
# pinned dependencies and a test suite, was recorded UNBUILDABLE for the single
# reason that `pkg-config --exists mysqlclient` failed. Nothing about that
# repository is broken. An image without these libraries does not measure how
# much of the Python ecosystem is repairable; it measures its own contents.
#
# The list stays short and every entry is a package a popular application
# actually needs. It is not "install everything in case" — that would trade a
# false UNBUILDABLE for a twenty-minute image build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates \
      build-essential pkg-config \
      default-libmysqlclient-dev \
      libpq-dev \
      libxml2-dev libxslt1-dev \
      libffi-dev libssl-dev \
      libjpeg-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# pyproject declares no `readme`, so the file is not strictly required to
# install — it is copied anyway because a build that breaks the moment somebody
# adds one line to pyproject is not worth the saved second.
COPY pyproject.toml README.md ./
COPY packages/ packages/
RUN pip install -e .

COPY services/ services/
COPY scripts/ scripts/

# Probing clones into a scratch directory and deletes it after each repository.
# It is a plain unprivileged user rather than root because the suites we run are
# other people's code, and a `conftest.py` runs at collection time.
RUN useradd --create-home --uid 1000 nightshift \
 && mkdir -p /app/fleet /app/benchmark \
 && chown -R nightshift:nightshift /app
USER nightshift

ENTRYPOINT ["python", "scripts/probe_fleet.py"]
CMD ["--out", "benchmark/cases.json"]
