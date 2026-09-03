#!/bin/bash
# Stop the local MongoDB/Redis test services started by start_mongo.sh /
# start_redis.sh.
set +e

docker stop bluesky-tiled-plugins-test-mongo 2>/dev/null
docker stop bluesky-tiled-plugins-test-redis 2>/dev/null
unset TEST_MONGO_URI TEST_REDIS_URI
