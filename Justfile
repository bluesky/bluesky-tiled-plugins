docs:
  rm -rf docs/_build && rm -rf docs/_api && uv run --group docs sphinx-autobuild -nT docs docs/_build/html

# Start MongoDB and Redis docker containers for local testing of TiledInserter.
# After running this, set TEST_MONGO_URI / TEST_REDIS_URI in your shell (see
# the script output) before invoking pytest, or simply use `just test`.
services-up:
  bash continuous_integration/scripts/start_mongo.sh
  bash continuous_integration/scripts/start_redis.sh

services-down:
  bash continuous_integration/scripts/stop_services.sh

# Run the full test suite against locally-running Mongo and Redis services.
test *args:
  TEST_MONGO_URI=mongodb://localhost:27017 \
  TEST_REDIS_URI=redis://localhost:6379/0 \
  uv run pytest {{args}}
