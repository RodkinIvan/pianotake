# Building the macOS app

`build.sh` turns pianotake into `dist/pianotake.app` and
`dist/pianotake-<version>-macos-<arch>.dmg`. Users then need no Python.

```bash
pip install pyinstaller .            # in the Python you build with
packaging/macos/build.sh             # PYTHON=/path/to/python to pick another one
```

Without a certificate the app is **ad-hoc signed**. It runs on your own Mac. On other
Macs, Gatekeeper refuses it on first launch. Users then click **Open Anyway** under System
Settings → Privacy & Security, or run `xattr -dr com.apple.quarantine pianotake.app`.

The app declares `NSMicrophoneUsageDescription`, so on first use macOS asks whether
*pianotake* may use audio input. The user doesn't have to grant permission to a terminal.

## Signed and notarized builds

That's what makes macOS say *"Apple checked it for malicious software and none was
detected."* It is a one-time setup:

1. **Join the Apple Developer Program** (developer.apple.com/programs, $99/year).
2. **Create a Developer ID Application certificate.** Open Keychain Access, then
   Certificate Assistant → *Request a Certificate From a Certificate Authority* → save to
   disk. At developer.apple.com go to Certificates → **+** → *Developer ID Application*,
   upload the request, and download the certificate. Double-click it to install it. Check with:
   ```bash
   security find-identity -v -p codesigning
   ```
   It should list `Developer ID Application: Your Name (TEAMID)`.
3. **Store notarization credentials once.** Create an app-specific password at
   appleid.apple.com (Sign-In and Security → App-Specific Passwords), then run:
   ```bash
   xcrun notarytool store-credentials pianotake-notary --apple-id YOU@EXAMPLE.COM --team-id TEAMID
   ```
   It asks for the app-specific password and keeps it in your keychain.
4. **Build:**
   ```bash
   PIANOTAKE_SIGN_ID="Developer ID Application: Your Name (TEAMID)" \
   PIANOTAKE_NOTARY_PROFILE=pianotake-notary \
   packaging/macos/build.sh
   ```
   The script signs every binary with the hardened runtime and `entitlements.plist`. It
   notarizes and staples the app, then builds, signs, notarizes and staples the DMG, and
   finishes with a Gatekeeper check (`spctl`), which must say `source=Notarized Developer ID`.

## Releases from GitHub Actions

`.github/workflows/macos-app.yml` builds the DMG by hand (*Run workflow*) or for a pushed
tag (`git tag v0.1.0 && git push origin v0.1.0`). A tag also publishes a GitHub Release
with the DMG attached. To sign and notarize there, add these repository secrets
(Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `MACOS_CERT_P12` | `base64 -i cert.p12 \| pbcopy`: export the Developer ID certificate *with its private key* from Keychain Access as .p12 |
| `MACOS_CERT_PASSWORD` | the password you gave that .p12 |
| `MACOS_SIGN_ID` | `Developer ID Application: Your Name (TEAMID)` |
| `NOTARY_KEY_P8` | contents of an App Store Connect API key (.p8): App Store Connect → Users and Access → Integrations → Keys, role *Developer* |
| `NOTARY_KEY_ID` | that key's ID |
| `NOTARY_ISSUER` | the Issuer ID shown above the keys |

## Notes

- The build matches the architecture of the Python you use (Apple Silicon on an M-series Mac).
- `entitlements.plist` allows audio input under the hardened runtime, plus the
  executable-memory exception that cffi callbacks (sounddevice, soundfile) need.
- The default bundle ID is `io.github.rodkinivan.pianotake`. Forks should set their own
  with `PIANOTAKE_BUNDLE_ID`.
