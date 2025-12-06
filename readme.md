# yt-dlp Music TUI

A terminal-based YouTube Music player that plays audio-only streams using `mpv` and `yt-dlp`. It filters out non-music content (like reactions, tutorials, or long videos), provides smooth playback with a progress bar, and automatically builds a queue of similar tracks.

## How It Works

- Uses `yt-dlp` to search and fetch YouTube video metadata.
- Filters results to prefer official music videos and exclude non-music content.
- Plays audio through `mpv` in the background (no video).
- Communicates with `mpv` using a Unix domain socket for real-time playback control (pause, volume, seek).
- Maintains a play history and auto-generates a queue using YouTube's auto-mix feature.
- Renders a terminal user interface (TUI) with `curses` showing current track, progress, and upcoming songs.

## Requirements

- Python 3.7 or newer
- `mpv` (media player)
- `yt-dlp` (video downloader)
- `socat` is **not** required (this version uses direct socket communication)

> Note: This program does **not** download or save audio files. It streams audio directly from YouTube.

## Installation

1. **Install system dependencies**

   On Debian/Ubuntu:
   ```bash
   sudo apt install mpv python3 python3-pip
   ```

   On Arch/Manjaro:
   ```bash
   sudo pacman -S mpv python
   ```

2. **Install Python dependencies**
   ```bash
   pip3 install yt-dlp
   ```

3. **Download the script**
   ```bash
   git clone https://github.com/exar1o/yt-music-tui.git
   ```
   Or, if you have the file locally, skip this step.

4. **Make it executable (optional)**
   ```bash
   cd yt-music-tui
   chmod +x yt_music_tui.py
   ```

## Basic Usage

1. Run the program:
   ```bash
   python3 yt_music_tui.py
   ```

2. On first launch, it may show a warning about missing YouTube cookies.  
   - Press any key to continue without cookies (some videos may not play).  
   - Press **`S`** to skip this warning for the rest of the session.

3. Press **`/`** to search for a song or artist.

4. Use the following keys during playback:
   - **Space**: Pause/resume
   - **`n`**: Skip to next track
   - **`s`**: Skip to a specific track number in the queue
   - **`j` / `k`**: Increase/decrease volume
   - **`,` / `.`**: Seek backward/forward by 10 seconds
   - **`h`**: Toggle shuffle
   - **`r`**: Cycle repeat mode (off → one → all)
   - **`c`**: Clear the queue
   - **`q`**: Quit

## Optional: Add YouTube Cookies (for age-restricted content)

Some videos require being logged into YouTube. To enable access:

1. Install the **"cookies.txt"** extension in your browser.
2. Go to [youtube.com](https://www.youtube.com) and log in.
3. Click the extension icon and choose **"Export"**.
4. Save the file as `~/.yt-dlp-cookies.txt`.
5. Set the environment variable (optional, `yt-dlp` will find it automatically):
   ```bash
   export YT_DLP_COOKIES=~/.yt-dlp-cookies.txt
   ```

After this, restart the program. The cookie warning will no longer appear.

## Notes

- The program stores up to 100 played tracks in `~/.cache/yt_music_history.json`.
- The queue automatically refills when it gets low.
- Maximum track duration is 6 minutes by default (configurable in source).

## Troubleshooting

- If you see **"No results"**, try a more specific search query.
- If playback fails repeatedly, ensure you have an internet connection and consider adding cookies.
- If the TUI appears broken, make sure your terminal supports UTF-8 and has at least 80x24 size.

---