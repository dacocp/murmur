#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p dist/server/dist
python3 ../release.py --build-manifest dist/build-manifest.json >/dev/null
MURMUR_BUILD_REVISION=$(python3 -c 'import json; print(json.load(open("dist/build-manifest.json"))["revision"])')
MURMUR_BUILD_SOURCE=$(python3 -c 'import json; print(json.load(open("dist/build-manifest.json"))["source_sha256"])')
docker run --rm -e CGO_ENABLED=0 -e GOOS=linux -e GOARCH=amd64 \
  -e MURMUR_BUILD_REVISION="$MURMUR_BUILD_REVISION" -e MURMUR_BUILD_SOURCE="$MURMUR_BUILD_SOURCE" \
  -v "$PWD:/src" -w /src -v "${MURMUR_GO_CACHE:-murmur-go-cache}:/go/pkg" \
  golang:1.26.10 sh -c 'go test -mod=readonly ./... && go build -mod=readonly -trimpath -ldflags "-X main.buildRevision=$MURMUR_BUILD_REVISION -X main.buildSourceHash=$MURMUR_BUILD_SOURCE" -o dist/server/dist/plugin-linux-amd64 .'
cp plugin.json dist/plugin.json
python3 ../webapp/build.py
cp dist/build-manifest.json dist/build.json
tar -czf dist/murmur-0.1.0.tar.gz -C dist plugin.json server webapp public build.json
python3 ../release.py --finish-build dist/murmur-0.1.0.tar.gz >/dev/null
