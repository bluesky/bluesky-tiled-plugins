#!/bin/bash
# Start a Redis service for local testing.
#
# When the tests see `TEST_REDIS_URI` they will exercise the
# `RedisJSONDict`-backed backup pathway against a real Redis instance.
#
#   $ source continuous_integration/scripts/start_redis.sh
#   $ pytest -v tests/test_tiled_inserter.py
set -e

docker run -d --rm \
    --name bluesky-tiled-plugins-test-redis \
    -p 6379:6379 \
    docker.io/redis:7-alpine
docker ps

export TEST_REDIS_URI="redis://localhost:6379/0"
echo "TEST_REDIS_URI=${TEST_REDIS_URI}"
