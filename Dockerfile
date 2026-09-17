# The test bot, so it can run on the server instead of someone's desktop.
#
# Dependencies are baked in; the CODE is bind-mounted. That split is deliberate:
# this image exists to iterate on a branch, and rebuilding an image for every
# edit would make "try it on the test bot" a slow step instead of a fast one.
# Pipfile.lock changing is the one thing that needs a rebuild.
FROM python:3.11-slim

# git: the bot reads its own revision for the /version footer.
# gcc + libffi: wheels that still build from source on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pipenv

WORKDIR /app
COPY Pipfile Pipfile.lock ./
# --system: into the image's own python, not a virtualenv inside /app, which the
# code bind-mount would hide. --deploy refuses a lock file that has drifted from
# the Pipfile rather than quietly resolving something else.
RUN pipenv install --system --deploy --ignore-pipfile

# greenlet is required by SQLAlchemy's ASYNC engine, which this bot uses for
# every query -- but the Pipfile asks for plain `sqlalchemy`, not
# `sqlalchemy[asyncio]`, so nothing declares it and it is absent from
# Pipfile.lock. Existing environments have it by accident, from some earlier
# resolution; a clean install from the lock does not, and the bot dies on its
# first query with "the greenlet library is required to use this function".
# Pinned to the version the working environments actually run. The real fix is
# the asyncio extra in the Pipfile, which is a relock and belongs in its own
# change rather than riding along with a container.
RUN pip install --no-cache-dir greenlet==3.2.3

# Migrations before the bot, exactly as systemd does it in production. Failing
# here stops the container rather than starting a bot against an old schema.
CMD ["sh", "-c", "alembic upgrade head && exec python bot.py"]
