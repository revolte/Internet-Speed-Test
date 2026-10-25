#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=1.0.0
OUTPUT_DIR="$PROJECT_DIR/dist"
PACKAGE_FILE="$OUTPUT_DIR/internet-speed-test_${VERSION}_all.deb"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM

mkdir -p \
    "$STAGE/DEBIAN" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/share/internet-speed-test" \
    "$STAGE/usr/share/applications"

install -m 0644 "$PROJECT_DIR/main.py" "$STAGE/usr/share/internet-speed-test/main.py"
install -m 0644 "$PROJECT_DIR/packaging/internet-speed-test.desktop" \
    "$STAGE/usr/share/applications/internet-speed-test.desktop"

cat > "$STAGE/usr/bin/internet-speed-test" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/share/internet-speed-test/main.py "$@"
EOF
chmod 0755 "$STAGE/usr/bin/internet-speed-test"
find "$STAGE" -type d -exec chmod 0755 {} +

cat > "$STAGE/DEBIAN/control" <<EOF
Package: internet-speed-test
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3, python3-tk, speedtest-cli, ca-certificates
Maintainer: Internet Speed Test contributors
Description: Desktop internet speed test
 A graphical utility to measure download and upload speeds using nearby
 Speedtest servers and keep a local history of completed tests.
EOF

mkdir -p "$OUTPUT_DIR"
dpkg-deb --root-owner-group --build "$STAGE" "$PACKAGE_FILE"
printf 'Built package: %s\n' "$PACKAGE_FILE"
