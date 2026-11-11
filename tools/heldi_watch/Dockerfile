# heldi-health-eval: the watch module on top of the EXACT live controller image (by digest), so the STOP
# acceptance, CURRENT and release verification run the very code of the controller they observe.
# Build at repository root:
#   docker build --build-arg CONTROLLER_IMAGE=<tenant-backfill-controller@sha256:...> -f tools/heldi_watch/Dockerfile .
# Rebuild on every controller release; a stale watcher reports DEGRADED WATCH_IMAGE_STALE instead of judging.
ARG CONTROLLER_IMAGE
FROM ${CONTROLLER_IMAGE}
COPY tools/heldi_watch /app/tools/heldi_watch
CMD ["python", "-m", "tools.heldi_watch.health"]
