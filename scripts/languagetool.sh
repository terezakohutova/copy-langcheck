# LanguageTool container for the copy-langcheck action. Sourced by the "Check the copy" step:
#
#   trap lt_stop EXIT; lt_start || echo "did not come up"
#
# lt_start reuses a healthy server already on localhost:8081 (a service container or an
# earlier step). Otherwise it starts one under a name unique to this run, attempt, step and
# inputs, on a port Docker picks, so two uses in one job or two jobs on one runner never
# collide. lt_stop removes only a container lt_start started. LT_URL is set either way.

LT_IMAGE='erikvl87/languagetool:6.8@sha256:ef8fa12cbd485166c9ceeb7139d76d56d07707a624da6bb1fc1fbb5411750527'
LT_DEFAULT_URL='http://localhost:8081/v2/check'
LT_CONTAINER=''

lt_healthy() {
  curl -sf --max-time 5 "$1?language=en-GB&text=Test." >/dev/null 2>&1
}

lt_name() {
  local key
  key=$(printf '%s\n' "${GITHUB_JOB:-}" "${GITHUB_ACTION:-}" "${INPUT_FILES:-}" "${INPUT_LANG:-}" \
        "${INPUT_FORCE_LANG:-}" "$$" | sha256sum | cut -c1-12)
  echo "copy-langcheck-lt-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$key"
}

lt_start() {
  if lt_healthy "$LT_DEFAULT_URL"; then
    LT_URL="$LT_DEFAULT_URL"
    echo "Using the LanguageTool server already running on port 8081"
    return 0
  fi
  LT_CONTAINER=$(lt_name)
  if ! docker run -d --rm --name "$LT_CONTAINER" -p 127.0.0.1::8010 "$LT_IMAGE" >/dev/null; then
    LT_CONTAINER=''
    return 1
  fi
  local port i
  port=$(docker port "$LT_CONTAINER" 8010/tcp | head -n 1 | sed 's/.*://')
  LT_URL="http://127.0.0.1:$port/v2/check"
  for i in $(seq 1 60); do
    if lt_healthy "$LT_URL"; then
      echo "LanguageTool up on port $port after about $((i * ${LT_POLL:-2})) s"
      return 0
    fi
    sleep "${LT_POLL:-2}"
  done
  return 1
}

lt_stop() {
  if [ -n "$LT_CONTAINER" ]; then
    docker rm -f "$LT_CONTAINER" >/dev/null 2>&1 || true
    LT_CONTAINER=''
  fi
}
