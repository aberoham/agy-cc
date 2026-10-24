# The fake agy inside a real Linux container, for tests/test_real_container.py.
# Build context: the tests directory.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 \
    && apt-get clean
COPY fake_agy.py /usr/local/bin/agy
RUN chmod 755 /usr/local/bin/agy
ENV FAKE_AGY_IN_CONTAINER=1 FAKE_AGY_SCENARIO=container PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["agy"]
