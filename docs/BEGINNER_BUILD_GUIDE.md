# Beginner guide: get your installers without coding

You will not compile anything on your computer. GitHub's free cloud computers build the files and you download them.

## One-time setup (about 10 minutes)

1. Go to github.com and create a free account.
2. Click the "+" at the top right, then "New repository".
3. Name it `trading-ops`. Choose **Private**. Click "Create repository".
4. Click "uploading an existing file". Unzip the project folder on your computer, then drag EVERYTHING inside the `mt5platform` folder into the page, including the hidden `.github` folder.
   - If the `.github` folder won't drag in, tell me and I will give you a one-file alternative.
5. Click "Commit changes".

## Build the installers

1. Click the **Actions** tab. If asked, click "I understand my workflows, go ahead and enable them".
2. Click "Build installers" on the left, then **Run workflow**, then the green button.
3. Wait 10-25 minutes. A green tick means success. A red cross means a problem: click it, copy the error text and send it to me. I will fix the code.
4. Open the finished run, scroll to **Artifacts**, and download:
   - `TradingOps-Windows-Setup` (contains the .exe installer)
   - `TradingOps-Android-APK` (contains the .apk)

## Install

- **Windows:** unzip the download, double-click `TradingOps-Setup.exe`. Windows may show "unknown publisher". Click "More info" then "Run anyway" (the app is not code-signed).
- **Android:** send the .apk to your phone, open it, and allow "Install unknown apps" when asked.
- Install **Tailscale** on both the PC and the phone (free) so the phone can reach the PC securely. Sign in to the same account on both.
- Install **MetaTrader 5** from your broker on the PC.

## Safety rules for the first weeks

- Use a **demo account only**. Run it for at least two weeks.
- Do not give anyone your trading password. The app stores it encrypted on your PC only.
- Keep the PC plugged in. Use Ethernet if you can.
