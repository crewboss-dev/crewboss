# LAN mode in 5 minutes

For a home or office where you want other computers and phones on the same Wi-Fi or network to open Crewboss. Nothing to install on those
computers. You do need to be comfortable typing a few commands on the computer that will run Crewboss.

> LAN mode is for a local network. Don't expose it to the internet without first upgrading to your own web address with a signed certificate (Administration, then Front door). Then each person can also add a passkey, which is recommended for anything on the internet.

## Before you start

You need, on the computer that will run Crewboss (Linux, macOS, or Windows with WSL2; the launcher does not run in plain PowerShell): Git, Python 3.10 or newer, and Docker.

* Docker: <https://docs.docker.com/engine/install/> (Docker Desktop on macOS and Windows).
* On Linux, after installing Docker run `sudo usermod -aG docker $USER`, then log out and back in, so you can use Docker without `sudo`. Check: `docker compose version` prints 2 or higher and `docker run --rm hello-world` works.
* Git on Debian or Ubuntu: `sudo apt install git`.
* Install an authenticator app on your phone now (a free app that shows a new 6-digit code every 30 seconds; Google Authenticator and Microsoft Authenticator are common).

## 1. Install (on the computer that will run Crewboss)

```
git clone https://github.com/crewboss-dev/crewboss.git
cd crewboss
./crewboss install
```

When it asks **How will people open Crewboss?**, choose **2) On my home or office network**. It shows an address such as
`https://192.0.2.20`. Press Enter if that is right. When it finishes it prints the address and a one-time password. Copy both.

Tip: ask your router to always give this computer the same address (often called "DHCP reservation" or "address reservation").
Otherwise the address can change after a restart. If it does, run `./crewboss lan-address` on the server.

## 2. Open it from another computer or phone

Type the address into the browser. You will see a warning such as "Your connection isn't private". That is expected on your own network.

* **Edge or Chrome:** click **Advanced**, then **Continue to ... (unsafe)**.
* **Firefox:** click **Advanced...**, then **Accept the Risk and Continue**.
* **Safari (Mac or iPhone):** click **Show Details**, then **visit this website**.

The server's own screen also shows a short code (a fingerprint). If you want to be extra careful, check that the browser's certificate
details show the same code. The page `/trust` on the server explains how.

An honest note: **Chrome and Edge ask again after about a week** (or after the browser restarts). Firefox and Safari remember it.
It is the same click-through each time. Getting rid of the warning means giving Crewboss your own web address (see below).

## 3. Sign in

1. Username `owner` and the one-time password.
2. Choose your own password.
3. Scan the QR code with your authenticator app, then type the 6 digits it shows.
4. Save the recovery codes somewhere safe.

Next time: password plus a 6-digit code from the app.

## Later: get rid of the warning (optional)

In Crewboss open **Administration**, then **Front door**. Three steps: type your web address, point it at your internet connection and
forward port 80 on your router, then press **Check** and **Switch on**. You get a padlock, and each person can then also add a passkey (passwordless sign-in). Details: [getting-started.md](getting-started.md#10-give-it-your-own-web-address-optional).

Never open LAN mode to the internet as it is.

## Next: run your first agent

Crewboss does the work on a PC of yours (a "runner") that has Claude Code installed and signed in. This can be the same computer. On that PC:

1. Install the runner (Python 3.12 or newer and `pipx` needed; the full steps are in [getting-started.md](getting-started.md#4-enrol-a-runner)):
   ```
   pipx ensurepath          # then close this terminal and open a new one
   pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"
   ```
2. In Crewboss click the app name at the top left, choose **Administration**, then **Machines**, stay on the **Runners** tab and press **New enrolment code**. If it asks **Where is this computer?**, choose **On this network**.
3. On the PC run `crewboss-runner enrol '<code>'`. The code is long and contains `#`: copy all of it, in quotes. Do **not** add `--insecure-http`: it is only for the "Just on this computer" try-out, and your code already carries the server's certificate fingerprint.
4. Run `crewboss-runner run` and leave that window open; use a second window for `crewboss-runner status`.

Then make a project and start a job: [getting-started.md](getting-started.md#3-make-a-project).
