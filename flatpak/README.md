# ctxgate-proxy Flatpak Package

Packages the ctxgate-proxy ecosystem (proxy + worker + dashboard) as a Flatpak application with a GUI.

## What's Included

| Component | Port | Description |
|-----------|------|-------------|
| ctxgate-proxy | 9200 | Main LLM context management proxy |
| 4B Worker | — | Memory extraction worker |
| Dashboard | 9201 | Web GUI for health monitoring |

## Prerequisites

```bash
# Install Flatpak + builder
sudo apt install flatpak flatpak-builder

# Add Flathub
flatpak remote add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo

# Install the runtime
flatpak install flathub org.freedesktop.Platform//23.08
```

## Build & Install

```bash
cd /home/user/ctxproxy/flatpak
./build.sh
```

## Launch (GUI)

```bash
flatpak run com.ctxgate.proxy
```

This will:
1. Start the proxy (with auto-restart supervisor)
2. Start the 4B memory worker
3. Start the dashboard
4. Open your browser to http://127.0.0.1:9201

## Configuration

After first launch, edit the config:
```bash
nano ~/.var/app/com.ctxgate.proxy/config/ctxgate/.env
```

Or set environment variables before launch:
```bash
CTXGATE_DB_DSN=postgresql://... flatpak run com.ctxgate.proxy
```

## Uninstall

```bash
flatpak uninstall com.ctxgate.proxy
```
