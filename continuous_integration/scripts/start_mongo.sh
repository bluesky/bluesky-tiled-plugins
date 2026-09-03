#!/bin/bash
# Start a MongoDB service for local testing.
#
# When the tests see `TEST_MONGO_URI` they will be parametrized to run
# against a real MongoDB instance in addition to the in-process mongomock
# adapter. Example:
#
#   $ source continuous_integration/scripts/start_mongo.sh
#   $ pytest -v tests/test_tiled_inserter.py
set -e

docker run -d --rm \
    --name bluesky-tiled-plugins-test-mongo \
    -p 27017:27017 \
    docker.io/mongo:7
docker ps

export TEST_MONGO_URI="mongodb://localhost:27017"
echo "TEST_MONGO_URI=${TEST_MONGO_URI}"
