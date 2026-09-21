# syntax=docker/dockerfile:1.7
#
# The skill runner image. One container per function call, started with
# `--rm`, destroyed when the call returns.
#
# Both language runtimes live in one image so the sandbox service pulls and
# caches a single tag. The extra ~50 MB of Node in a Python-only deployment is
# cheaper than the operational cost of keeping two images in step, and the
# image is started often enough that a warm local cache matters more than size.
#
# There is no shell entry point and no server: the only way in is a JSON job on
# stdin, and the only way out is a JSON result on stdout.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node's built-in fetch ignores HTTP_PROXY/HTTPS_PROXY, so a JavaScript skill
# on an allowlisted host would silently try to connect directly — and fail,
# because the egress network has no route out. undici's ProxyAgent is how
# entrypoint.js routes fetch through the sandbox proxy instead.
RUN npm install --global --omit=dev undici@6.21.0 \
    && npm cache clean --force
# require() does not search the global root on its own.
ENV NODE_PATH=/usr/local/lib/node_modules

# requests is the one convenience a skill author reliably expects; urllib alone
# makes every HTTP skill unpleasant to write. Nothing else is preinstalled —
# a skill that needs more belongs in a service, not in the sandbox.
RUN pip install --no-cache-dir requests==2.32.3

RUN useradd -m -u 10001 runner
WORKDIR /runner
COPY entrypoint.py entrypoint.js /runner/
RUN chmod 444 /runner/entrypoint.py /runner/entrypoint.js

USER runner

# The sandbox service selects the runtime per call by passing the command
# (`python /runner/entrypoint.py` or `node /runner/entrypoint.js`). That has to
# override the default, so this is CMD with an emptied ENTRYPOINT — with an
# ENTRYPOINT set, `docker run <image> node …` appends to it instead of
# replacing it, and every JavaScript skill would be fed to Python.
ENTRYPOINT []
CMD ["python", "/runner/entrypoint.py"]
