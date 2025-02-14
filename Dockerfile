# Prepare the base environment.
FROM python:3.9.6-slim-buster as builder_base
MAINTAINER asi@dbca.wa.gov.au
LABEL org.opencontainers.image.source https://github.com/dbca-wa/resource_tracking

RUN apt-get update -y \
  && apt-get upgrade -y \
  && apt-get install --no-install-recommends -y wget git libmagic-dev gcc binutils gdal-bin proj-bin python3-dev \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --upgrade pip

# Install Python libs using Poetry.
FROM builder_base as python_libs
WORKDIR /app
ENV POETRY_VERSION=1.1.6
RUN pip install "poetry==$POETRY_VERSION"
RUN python -m venv /venv
COPY poetry.lock pyproject.toml /app/
RUN poetry config virtualenvs.create false \
  && poetry install --no-dev --no-interaction --no-ansi

# Install the project.
FROM python_libs
COPY gunicorn.py manage.py ./
COPY resource_tracking ./resource_tracking
COPY tracking ./tracking
RUN python manage.py collectstatic --noinput

# Run the application as the www-data user.
USER www-data
EXPOSE 8080
CMD ["gunicorn", "resource_tracking.wsgi", "--config", "gunicorn.py"]
