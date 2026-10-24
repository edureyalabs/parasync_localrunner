#!/usr/bin/env bash
# scripts/build_rootfs.sh
# Builds the image and exports it as dist/parasync-runner-rootfs-<version>.tar.gz plus dist/manifest.json (version, size, SHA-256,
# the cloud commit the harness came from). The desktop app downloads the archive, checks the SHA-256 against the manifest it was built
# with, and imports it with `wsl --import`. Run from the repository root; needs Docker.
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION="$(cat VERSION)"
IMAGE="parasync-runner:${VERSION}"
ARCHIVE="dist/parasync-runner-rootfs-${VERSION}.tar.gz"

mkdir -p dist
docker build -f image/Dockerfile -t "${IMAGE}" .
CID="$(docker create "${IMAGE}")"
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
docker export "${CID}" | gzip -9 > "${ARCHIVE}"

SHA="$(sha256sum "${ARCHIVE}" | cut -d' ' -f1)"
SIZE="$(stat -c%s "${ARCHIVE}")"
HARNESS="$(python3 -c "import json; print(json.load(open('runner/SYNCED_FROM.json'))['commit'])")"
cat > dist/manifest.json <<JSON
{
  "version": "${VERSION}",
  "file": "$(basename "${ARCHIVE}")",
  "sha256": "${SHA}",
  "bytes": ${SIZE},
  "harnessCommit": "${HARNESS}",
  "builtAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "Built ${ARCHIVE} (${SIZE} bytes, sha256 ${SHA})"
