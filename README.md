# vo-tui: real-time English to Russian translator

<img width="825" height="520" alt="vo-tui screenshot" src="https://github.com/user-attachments/assets/a4306153-fc15-4c87-8dab-21889f9a0b93" />

🇺🇸→🇷🇺

`vo-tui` is a macOS terminal UI for real-time English transcription and on-device Russian translation, powered by [k1LoW's vo](https://github.com/k1LoW/vo).

It listens to your Mac's system audio, shows the English transcript and Russian translation side by side, and keeps the audio and translation on your device. No API key or cloud service is required.

## 🍺 Have Homebrew?

Install and run `vo-tui` with two command:

1. Trust the vo dependency (required by Homebrew once)
```bash
brew trust --formula k1low/tap/vo
```
2. Install this tui
```bash
brew install policememos/tap/vo-tui
```
3. Run
```bash
vo-tui
```

## 🥃 Install manually

1. On GitHub, click **Code → Download ZIP** and unpack the archive.
2. Open Terminal in the unpacked `vo-tui` folder.
3. Run:

```bash
python3 vo_tui.py
```

## 💡 After installation

Just press `Space` to start.

On first launch, `vo-tui` can install its dependency automatically and macOS may request Audio Recording and Speech Recognition permissions.
