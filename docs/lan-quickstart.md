# LAN mode in 5 minutes

For a home or office where you want other computers and phones on the same Wi-Fi or network to open Crewboss. No IT knowledge needed,
and nothing to install on those computers.

> LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed certificate and passkey sign-in.

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
3. Scan the QR code with an authenticator app on your phone, then type the 6 digits it shows.
4. Save the recovery codes somewhere safe.

Next time: password plus a 6-digit code from the app.

## Later: get rid of the warning (optional)

In Crewboss open **Administration**, then **Front door**. Three steps: type your web address, point it at your internet connection and
forward port 80 on your router, then press **Check** and **Switch on**. You get a padlock and the option of passwordless sign-in with a
passkey. Details: [getting-started.md](getting-started.md#10-give-it-your-own-web-address-optional).

Never open LAN mode to the internet as it is.
