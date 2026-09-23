#!/bin/bash
# Sign MinKingAI.app. Developer ID + notarization when Apple secrets are set.
# Otherwise ad-hoc sign so a local Mac can still open the bundle.
set -euo pipefail

APP="dist/MinKingAI.app"
ENTITLEMENTS="entitlements.plist"
IDENTITY="${APPLE_SIGN_IDENTITY:-}"
SIGN_MODE="adhoc"

if [[ ! -d "$APP" ]]; then
  echo "missing $APP" >&2
  exit 1
fi

sign_adhoc() {
  codesign --force --deep --sign - "$APP"
  echo "signed ad-hoc"
}

import_certificate() {
  local keychain="$RUNNER_TEMP/minking-signing.keychain"
  local keychain_password
  keychain_password="$(openssl rand -hex 16)"
  echo "$APPLE_CERTIFICATE_P12" | base64 --decode > "$RUNNER_TEMP/certificate.p12"
  security create-keychain -p "$keychain_password" "$keychain"
  security set-keychain-settings -lut 21600 "$keychain"
  security unlock-keychain -p "$keychain_password" "$keychain"
  security import "$RUNNER_TEMP/certificate.p12" -k "$keychain" -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain" >/dev/null
  security list-keychains -d user -s "$keychain" $(security list-keychains -d user | tr -d '"')
  local listed
  listed="$(security find-identity -v -p codesigning "$keychain")"
  IDENTITY="$(printf '%s\n' "$listed" | awk -F'"' '/Developer ID Application/{print $2; exit}')"
  if [[ -n "$IDENTITY" ]]; then
    SIGN_MODE="developer-id"
  else
    IDENTITY="$(printf '%s\n' "$listed" | awk -F'"' 'NF > 1 {print $2; exit}')"
    SIGN_MODE="self-signed"
  fi
  rm -f "$RUNNER_TEMP/certificate.p12"
  if [[ -z "$IDENTITY" ]]; then
    echo "no code signing identity was imported" >&2
    exit 1
  fi
}

sign_with_identity() {
  local file timestamp=()
  if [[ "$SIGN_MODE" == "developer-id" ]]; then
    timestamp=(--timestamp)
  fi
  while IFS= read -r -d '' file; do
    if file "$file" | grep -q "Mach-O"; then
      codesign --force --options runtime "${timestamp[@]}" --sign "$IDENTITY" "$file"
    fi
  done < <(find "$APP/Contents" -type f -print0)
  codesign --force --options runtime "${timestamp[@]}" --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
  echo "signed $SIGN_MODE"
}

notarize_app() {
  local archive="$RUNNER_TEMP/MinKingAI-notarize.zip"
  ditto -c -k --keepParent "$APP" "$archive"
  xcrun notarytool submit "$archive" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_SPECIFIC_PASSWORD" \
    --wait
  xcrun stapler staple "$APP"
  rm -f "$archive"
  echo "notarized"
}

if [[ -n "${APPLE_CERTIFICATE_P12:-}" && -n "${APPLE_CERTIFICATE_PASSWORD:-}" ]]; then
  import_certificate
  sign_with_identity
  if [[ "$SIGN_MODE" == "developer-id" && -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
    notarize_app
  elif [[ "$SIGN_MODE" == "self-signed" ]]; then
    echo "self-signed; this Mac can codesign with it, Gatekeeper will not treat it as notarized"
  else
    echo "developer id signed; notarization secrets are absent"
  fi
else
  sign_adhoc
fi
