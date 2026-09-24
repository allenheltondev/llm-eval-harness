#!/usr/bin/env bash
#
# Assembles the cloud eval worker's Lambda zip artifact.
#
# The zip must already contain every dependency -- nothing is installed at
# deploy time -- and the function is arm64, so this builds a staging tree of
# aarch64-manylinux wheels, drops the `nimbus` package next to them, and
# zips it under a content-hashed key. No root-level entry module: the template
# names `nimbus.worker.lambda_app.handler` directly.
#
# The key is content-hashed on purpose. For an S3-sourced function the key IS
# the property CloudFormation watches, so a static key means CFN sees no change
# and the old bundle keeps serving forever. A hash-versioned key makes every
# real code change a visible property change. (This is rsc-core's
# AgentArtifactKey lesson, applied to Python.)
#
# Usage:
#   scripts/package-eval-worker.sh              # build into .build/eval-worker
#   EVAL_WORKER_BUILD_DIR=/tmp/x scripts/...    # build somewhere else
#
# Writes <build-dir>/artifact.env with ARTIFACT_ZIP / ARTIFACT_SHA /
# ARTIFACT_KEY for `make deploy-backend` to source. Makes no AWS calls.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="${ROOT}/server"
BUILD_DIR="${EVAL_WORKER_BUILD_DIR:-${ROOT}/.build/eval-worker}"
STAGING="${BUILD_DIR}/staging"

# Must match the template's `Runtime: python3.12` and `Architectures: [arm64]`.
# Wheels are resolved for that platform regardless of this build machine's.
PYTHON_VERSION="3.12"
PYTHON_PLATFORM="${EVAL_WORKER_PYTHON_PLATFORM:-aarch64-manylinux_2_28}"

# Packages pulled in transitively that no evaluation path imports, and which
# together are a quarter of the artifact. Lambda's ceiling is 250 MB *unzipped*
# across the function and its layers -- a limit this artifact did not have on
# its previous host, which allowed 750 MB, so the margin here is real and worth
# keeping. Set EVAL_WORKER_PRUNE=0 to keep them and see the raw size.
PRUNE="${EVAL_WORKER_PRUNE:-1}"
PRUNE_DIRS=(sympy PIL pillow.libs mpmath)

log() { printf '\033[1m==>\033[0m %s\n' "$*" >&2; }

command -v uv >/dev/null || { echo "package-eval-worker: uv is required" >&2; exit 1; }
command -v zip >/dev/null || { echo "package-eval-worker: zip is required" >&2; exit 1; }

rm -rf "${STAGING}"
mkdir -p "${STAGING}" "${BUILD_DIR}"

# 1. Pin the dependency set from the server's lockfile. `--no-emit-project`
#    leaves `nimbus` itself out: it is pure Python and gets copied in
#    below, so there is no wheel build to cross-compile.
log "Exporting locked runtime dependencies"
uv export \
  --project "${SERVER}" \
  --frozen \
  --no-dev \
  --no-emit-project \
  --no-hashes \
  --format requirements-txt \
  > "${BUILD_DIR}/requirements.txt"

# 2. Install them for the runtime's platform. `--only-binary=:all:` is the
#    safety net: a source distribution would be built for *this* machine's
#    architecture and fail at import inside the arm64 runtime, so the build
#    fails loudly here instead.
log "Installing wheels for ${PYTHON_PLATFORM} (py${PYTHON_VERSION})"
uv pip install \
  --python-platform "${PYTHON_PLATFORM}" \
  --python-version "${PYTHON_VERSION}" \
  --target "${STAGING}" \
  --only-binary=:all: \
  --no-installer-metadata \
  --requirement "${BUILD_DIR}/requirements.txt"

# 3. The application itself.
log "Staging the nimbus package"
cp -R "${SERVER}/nimbus" "${STAGING}/nimbus"
# The pre-rename handler path, for the deploy that switches Handler to nimbus
# (server/compat/evalharness/__init__.py says why). Delete in the next release.
cp -R "${SERVER}/compat/evalharness" "${STAGING}/evalharness"

# 4. Strip build noise so the hash reflects source, not incidental state.
find "${STAGING}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${STAGING}" -type f -name '*.pyc' -delete
find "${STAGING}" -type d -name 'tests' -path '*/site-packages/*' -prune -exec rm -rf {} + 2>/dev/null || true
# uv drops a `.lock` in --target directories; it is build-machine state, not code.
rm -f "${STAGING}/.lock"

if [ "${PRUNE}" != "0" ]; then
  for dir in "${PRUNE_DIRS[@]}"; do
    if [ -e "${STAGING}/${dir}" ]; then
      log "  dropping ${dir} ($(du -sh "${STAGING}/${dir}" | cut -f1)) -- unused by any evaluation path"
      rm -rf "${STAGING:?}/${dir}"
    fi
  done
fi

UNZIPPED_KB="$(du -sk "${STAGING}" | cut -f1)"

# 5. Zip deterministically enough that an unchanged tree hashes the same:
#    sorted entries, no extra attributes, timestamps normalised.
log "Zipping"
STAGED_ZIP="${BUILD_DIR}/eval-worker.zip"
rm -f "${STAGED_ZIP}"
find "${STAGING}" -exec touch -t 198001010000 {} + 2>/dev/null || true
( cd "${STAGING}" && find . -type f -o -type l | LC_ALL=C sort | zip -qX "${STAGED_ZIP}" -@ )

ARTIFACT_SHA="$(sha256sum "${STAGED_ZIP}" | cut -c1-16)"
ARTIFACT_KEY="eval-worker/${ARTIFACT_SHA}.zip"
ARTIFACT_ZIP="${BUILD_DIR}/${ARTIFACT_SHA}.zip"
mv -f "${STAGED_ZIP}" "${ARTIFACT_ZIP}"

cat > "${BUILD_DIR}/artifact.env" <<ENV
ARTIFACT_ZIP=${ARTIFACT_ZIP}
ARTIFACT_SHA=${ARTIFACT_SHA}
ARTIFACT_KEY=${ARTIFACT_KEY}
ENV

log "Artifact: ${ARTIFACT_ZIP} ($(du -h "${ARTIFACT_ZIP}" | cut -f1) zipped, $((UNZIPPED_KB / 1024)) MB unzipped)"
log "S3 key:   ${ARTIFACT_KEY}"

# Lambda's ceiling is 250 MB unzipped across the function and every layer it
# attaches. Fail loudly here rather than at deploy time.
if [ "${UNZIPPED_KB}" -gt 245760 ]; then
  echo "package-eval-worker: unzipped size $((UNZIPPED_KB / 1024)) MB is at/over Lambda's 250 MB limit." >&2
  echo "                     Prune further (see PRUNE_DIRS in this script) before deploying." >&2
  exit 1
fi
