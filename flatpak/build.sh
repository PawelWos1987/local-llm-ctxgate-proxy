#!/bin/bash
# Build and install ctxgate-proxy as a Flatpak
set -e

cd "$(dirname "$0")"

echo "=== Building ctxgate-proxy Flatpak ==="

# Option 1: Using flatpak-builder (recommended)
if command -v flatpak-builder &> /dev/null; then
    echo "Using flatpak-builder..."
    
    # Create build directory
    BUILD_DIR="/tmp/ctxgate-flatpak-build"
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR"
    
    # Copy sources into build directory
    cp -r ../proxy ../worker ../dashboard ../schema "$BUILD_DIR/"
    cp ../requirements.txt "$BUILD_DIR/"
    cp ../.env.example "$BUILD_DIR/"
    cp ../supervisor.sh ../start_proxy.sh "$BUILD_DIR/"
    cp manifest.yaml "$BUILD_DIR/"
    
    # Build
    flatpak-builder --force-clean --user --install --rebuild "$BUILD_DIR" manifest.yaml
    
    echo ""
    echo "=== Installed! ==="
    echo "Launch with: flatpak run com.ctxgate.proxy"
    echo "Or from your application menu: 'ctxgate-proxy'"
else
    echo "ERROR: flatpak-builder not found."
    echo "Install it with:"
    echo "  sudo apt install flatpak flatpak-builder"
    echo "  flatpak remote add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo"
    echo "  flatpak install flathub org.freedesktop.Platform//23.08"
    exit 1
fi
