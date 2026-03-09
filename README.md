### What does this script do
This script connects to OBS Studio via WebSocket and shows NVIDIA App-like toast notifications for the following events:
- Recording has started
- Saving recording
- Recording saved

### Requirements
A `.env` file with the following variables set:

```env
HOST=
PORT=
PASSWORD=
```

### Notes
- The toast window is designed to work best with borderless/windowed games.
- In true exclusive fullscreen mode, regular desktop windows may appear behind the game.
- The screen resolution and toast position are currently hardcoded in pixels. If your display setup differs, you may need to adjust the window size, coordinates, and related layout constants manually.
