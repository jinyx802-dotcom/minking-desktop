#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-}"
shift || true
PROMPT=""; IMAGE=""; REFERENCE=""; VIDEO_ID=""; OUT_DIR=""; SIZE=""; SECONDS_VAL="4"
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --reference) REFERENCE="${2:-}"; shift 2 ;;
    --video-id) VIDEO_ID="${2:-}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    --size) SIZE="${2:-}"; shift 2 ;;
    --seconds) SECONDS_VAL="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
err() { printf '%s\n' "$*" >&2; }
cleanup() { [ -n "${SAFE_UPLOAD:-}" ] && rm -f "$SAFE_UPLOAD" || true; }
trap cleanup EXIT
if ! command -v curl >/dev/null 2>&1; then err "curl not found"; exit 1; fi

json_escape() {
  printf '%s' "$1" | awk 'BEGIN{ORS=""}{gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); gsub(/\r/,""); gsub(/\t/,"\\t"); n=split($0,a,"\n"); for(i=1;i<=n;i++){printf "%s",a[i]; if(i<n) printf "\\n"}}'
}
json_string() { printf '"%s"' "$(json_escape "$1")"; }
json_field() {
  local key="$1" text="$2"
  printf '%s' "$text" | awk -v k="$key" '{
    s=$0; p=index(s, "\"" k "\""); if(p==0) next
    rest=substr(s,p)
    if (match(rest, /"[^"]+"[ \t]*:[ \t]*"/)) {
      rest=substr(rest, RSTART+RLENGTH); out=""
      for(i=1;i<=length(rest);i++){ c=substr(rest,i,1); if(c=="\\"){out=out substr(rest,i,2); i++; continue} if(c=="\"") break; out=out c }
      print out; exit
    }
  }'
}
join_v1() { local base="${1%/}"; [ -z "$base" ] && { printf ''; return; }; case "$base" in */v1) printf '%s' "$base" ;; *) printf '%s' "$base/v1" ;; esac; }
HOME_DIR="${USERPROFILE:-${HOME:-}}"
read_env_file() {
  local path="$1" want="$2"; [ -f "$path" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in "#"*) continue ;; "${want}="*) printf '%s' "${line#*=}" | tr -d '"' | tr -d "'"; return 0 ;; esac
  done < "$path"
}

resolve_creds() {
  if [ -n "${OPENAI_API_KEY:-}" ] && [ -n "${OPENAI_BASE_URL:-}" ]; then KEY="$OPENAI_API_KEY"; V1="$(join_v1 "$OPENAI_BASE_URL")"; return 0; fi
  local envf="$HOME_DIR/.codex/.env" k b
  k="$(read_env_file "$envf" OPENAI_API_KEY || true)"; b="$(read_env_file "$envf" OPENAI_BASE_URL || true)"
  if [ -n "$k" ] && [ -n "$b" ]; then KEY="$k"; V1="$(join_v1 "$b")"; return 0; fi
  local toml="$HOME_DIR/.grok/config.toml"
  if [ -f "$toml" ]; then
    b="$(awk 'BEGIN{p=0} /^\[model_providers\.minkingapi\]/{p=1;next} /^\[/{if(p) exit} p && $1=="base_url"{gsub(/"/,""); print $3; exit}' "$toml")"
    k="$(awk 'BEGIN{p=0} /^\[model_providers\.minkingapi\]/{p=1;next} /^\[/{if(p) exit} p && $1=="api_key"{gsub(/"/,""); print $3; exit}' "$toml")"
    if [ -n "$k" ] && [ -n "$b" ]; then KEY="$k"; V1="$(join_v1 "$b")"; return 0; fi
  fi
  local claude="$HOME_DIR/.claude/settings.json"
  if [ -f "$claude" ]; then
    local raw; raw="$(tr -d '\n' < "$claude")"
    k="$(json_field ANTHROPIC_API_KEY "$raw")"; b="$(json_field ANTHROPIC_BASE_URL "$raw")"
    [ -z "$b" ] && b="$(json_field OPENAI_BASE_URL "$raw")"
    if [ -n "$k" ]; then KEY="$k"; V1="$(join_v1 "$b")"; return 0; fi
  fi
  local wb="$HOME_DIR/.workbuddy/models.json"
  if [ -f "$wb" ]; then
    local raw; raw="$(tr -d '\n' < "$wb")"
    k="$(json_field apiKey "$raw")"; b="$(json_field url "$raw")"
    if [ -n "$k" ] && [ -n "$b" ]; then KEY="$k"; V1="$(join_v1 "$b")"; return 0; fi
  fi
  err "MinKing API key not found. Run MinKing one-click sync first."; return 1
}

compact_body() { printf '%s' "$1" | tr '\n' ' ' | sed -E 's/data:image\/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+\/= ]+/data:image;base64,[redacted]/g' | cut -c1-360; }

throw_http() {
  local op="$1" status="$2" body="$3"
  local hint code advice
  hint="$(compact_body "$body")"
  code="$(json_field code "$body")"; [ -z "$code" ] && code="$(json_field message "$body")"
  advice=""
  if [ "$status" = "503" ] || printf '%s' "$hint" | grep -qi 'no_healthy_accounts\|cooldown'; then
    advice="Gateway cooldown. Wait 10 minutes. Do NOT retry."
  elif [ "$status" = "400" ] || [ "$status" = "413" ] || printf '%s' "$hint" | grep -qi 'length limit\|too large\|file_too_large'; then
    advice="Payload too large for Grok JSON inline. Use JPEG <=380KB. Do NOT retry the same PNG."
  elif [ "$status" -ge 500 ]; then
    advice="Upstream 5xx. Script retries once then stops."
  fi
  err "$op HTTP $status $hint $advice"
  exit 1
}

normalize_seconds() {
  local n="$1"
  case "$n" in 4|6|8) printf '%s' "$n" ;; 5|7) printf '6' ;; *) if [ "${n:-0}" -ge 7 ]; then printf '8'; else printf '4'; fi ;; esac
}

prepare_jpeg() {
  local src="$1" budget="$2" maxside="$3"
  [ -f "$src" ] || { err "reference image not found: $src"; exit 1; }
  local tmpdir="${TMPDIR:-/tmp}/minking-media"
  mkdir -p "$tmpdir"
  SAFE_UPLOAD="$tmpdir/ref-$$-$RANDOM.jpg"
  local size ext
  size="$(wc -c < "$src" | tr -d ' ')"
  ext="$(printf '%s' "$src" | tr 'A-Z' 'a-z')"
  case "$ext" in
    *.jpg|*.jpeg) if [ "$size" -le "$budget" ]; then cp "$src" "$SAFE_UPLOAD"; err "upload jpeg copy bytes=$size"; return 0; fi ;;
  esac
  local ff=""
  command -v ffmpeg >/dev/null 2>&1 && ff="ffmpeg"
  [ -z "$ff" ] && [ -x "/usr/bin/ffmpeg" ] && ff="/usr/bin/ffmpeg"
  if [ -z "$ff" ]; then err "no jpeg encoder found (ffmpeg). Pass a JPEG <=$budget bytes."; exit 1; fi
  local side q
  for side in "$maxside" 1024 768 640; do
    for q in 5 8 12 18; do
      "$ff" -y -hide_banner -loglevel error -i "$src" -vf "scale=${side}:${side}:force_original_aspect_ratio=decrease:force_divisible_by=2" -q:v "$q" "$SAFE_UPLOAD"
      size="$(wc -c < "$SAFE_UPLOAD" | tr -d ' ')"
      if [ "$size" -le "$budget" ] && [ "$size" -gt 1000 ]; then err "upload jpeg compressed bytes=$size side=$side"; return 0; fi
    done
  done
  err "could not compress reference under $budget bytes (got $size). Do not upload the original PNG."
  exit 1
}

http_json() {
  local method="$1" url="$2" body="${3:-}" outfile="${4:-}" timeout="${5:-240}"
  local hdr tmp; hdr="$(mktemp)"; tmp="$(mktemp)"
  local args=(-sS -X "$method" "$url" -H "Authorization: Bearer $KEY" -D "$hdr" --max-time "$timeout")
  if [ -n "$body" ]; then args+=(-H "Content-Type: application/json; charset=utf-8" --data-binary "$body"); fi
  if [ -n "$outfile" ]; then args+=(-o "$outfile"); else args+=(-o "$tmp"); fi
  curl "${args[@]}"
  STATUS="$(awk 'BEGIN{s=0} /^HTTP\//{s=$2} END{print s}' "$hdr")"
  BODY=""
  [ -z "$outfile" ] && BODY="$(cat "$tmp")"
  rm -f "$hdr" "$tmp"
}

http_form() {
  local url="$1" timeout="${2:-240}"; shift 2
  local hdr tmp; hdr="$(mktemp)"; tmp="$(mktemp)"
  curl -sS -X POST "$url" -H "Authorization: Bearer $KEY" -D "$hdr" --max-time "$timeout" "$@" -o "$tmp"
  STATUS="$(awk 'BEGIN{s=0} /^HTTP\//{s=$2} END{print s}' "$hdr")"
  BODY="$(cat "$tmp")"
  rm -f "$hdr" "$tmp"
}

http_retry() {
  local op="$1"; shift
  "$@"
  if [ "${STATUS:-0}" = "502" ] || [ "${STATUS:-0}" = "504" ]; then err "$op HTTP $STATUS, retry once after 3s"; sleep 3; "$@"; fi
  if [ "${STATUS:-0}" -ge 400 ]; then throw_http "$op" "$STATUS" "$BODY"; fi
}

save_image() {
  local dest="$1"; mkdir -p "$(dirname "$dest")"
  local url b64; url="$(json_field url "$BODY")"; b64="$(json_field b64_json "$BODY")"
  if [ -n "$url" ]; then
    http_json GET "$url" "" "$dest" 120
    [ "${STATUS:-0}" -ge 400 ] && { err "image download HTTP $STATUS"; return 1; }
    return 0
  fi
  if [ -n "$b64" ]; then printf '%s' "$b64" | base64 -d > "$dest" 2>/dev/null || printf '%s' "$b64" | base64 -D > "$dest"; return 0; fi
  err "image response had neither url nor b64_json"; return 1
}
wait_video() {
  local vid="$1" i=0 st
  while [ "$i" -lt 120 ]; do
    http_json GET "$V1/videos/$vid" "" "" 60
    [ "${STATUS:-0}" -ge 400 ] && throw_http "video poll" "$STATUS" "$BODY"
    st="$(json_field status "$BODY")"; err "video $vid $st"
    [ "$st" = "completed" ] && return 0
    [ "$st" = "failed" ] && { err "video failed"; return 1; }
    sleep 3; i=$((i+1))
  done
  err "video poll timed out"; return 1
}
save_video() {
  local vid="$1" dest="$2"; mkdir -p "$(dirname "$dest")"
  http_json GET "$V1/videos/$vid/content?variant=video" "" "$dest" 180
  [ "${STATUS:-0}" = "409" ] && return 1
  [ "${STATUS:-0}" -ge 400 ] && throw_http "video content" "$STATUS" ""
  return 0
}
abs_path() { local target="$1" dir; dir="$(cd "$(dirname "$target")" && pwd)"; printf '%s/%s\n' "$dir" "$(basename "$target")"; }

[ -z "$PROMPT" ] && { err "prompt is required"; exit 2; }
case "$COMMAND" in image|image-edit|video|video-edit) ;; *) err "usage: minking-media.sh image|image-edit|video|video-edit --prompt TEXT"; exit 2 ;; esac
resolve_creds
OUT_DIR="${OUT_DIR:-${MINKING_MEDIA_OUT:-$PWD}}"; mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
SECONDS_VAL="$(normalize_seconds "$SECONDS_VAL")"

if [ "$COMMAND" = "image" ]; then
  BODY_JSON="{\"prompt\":$(json_string "$PROMPT"),\"response_format\":\"url\""
  [ -n "$SIZE" ] && BODY_JSON="$BODY_JSON,\"size\":$(json_string "$SIZE")"
  BODY_JSON="$BODY_JSON}"
  http_retry "images/generations" http_json POST "$V1/images/generations" "$BODY_JSON"
  DEST="$OUT_DIR/minking-image-$STAMP.png"; save_image "$DEST"; abs_path "$DEST"; exit 0
fi

if [ "$COMMAND" = "image-edit" ]; then
  [ -z "$IMAGE" ] && { err "image-edit requires --image"; exit 2; }
  FORM=(--form-string "prompt=$PROMPT" --form-string "response_format=url")
  if [ -f "$IMAGE" ]; then
    prepare_jpeg "$IMAGE" 700000 1536
    FORM+=(-F "image=@${SAFE_UPLOAD};type=image/jpeg;filename=edit.jpg")
  else
    FORM+=(--form-string "image=$IMAGE")
  fi
  [ -n "$SIZE" ] && FORM+=(--form-string "size=$SIZE")
  http_retry "images/edits" http_form "$V1/images/edits" 240 "${FORM[@]}"
  DEST="$OUT_DIR/minking-image-$STAMP.png"; save_image "$DEST"; abs_path "$DEST"; exit 0
fi

if [ "$COMMAND" = "video" ]; then
  if [ -n "$REFERENCE" ] && [ -f "$REFERENCE" ]; then
    prepare_jpeg "$REFERENCE" 380000 1280
    FORM=(--form-string "prompt=$PROMPT" --form-string "seconds=$SECONDS_VAL" --form-string "model=grok-imagine-video-1.5" -F "input_reference=@${SAFE_UPLOAD};type=image/jpeg;filename=ref.jpg")
    [ -n "$SIZE" ] && FORM+=(--form-string "size=$SIZE")
    http_retry "videos" http_form "$V1/videos" 60 "${FORM[@]}"
  else
    BODY_JSON="{\"prompt\":$(json_string "$PROMPT"),\"model\":\"grok-imagine-video-1.5\",\"seconds\":$(json_string "$SECONDS_VAL")"
    [ -n "$SIZE" ] && BODY_JSON="$BODY_JSON,\"size\":$(json_string "$SIZE")"
    [ -n "$REFERENCE" ] && BODY_JSON="$BODY_JSON,\"input_reference\":$(json_string "$REFERENCE")"
    BODY_JSON="$BODY_JSON}"
    http_retry "videos" http_json POST "$V1/videos" "$BODY_JSON" "" 60
  fi
  VID="$(json_field id "$BODY")"; [ -z "$VID" ] && { err "videos response had no id"; exit 1; }
  wait_video "$VID"
  DEST="$OUT_DIR/minking-video-$STAMP.mp4"
  i=0
  while [ "$i" -lt 20 ]; do save_video "$VID" "$DEST" && { abs_path "$DEST"; exit 0; }; i=$((i+1)); sleep 3; done
  err "video content not ready"; exit 1
fi

if [ "$COMMAND" = "video-edit" ]; then
  [ -z "$VIDEO_ID" ] && { err "video-edit requires --video-id"; exit 2; }
  BODY_JSON="{\"prompt\":$(json_string "$PROMPT"),\"model\":\"grok-imagine-video\",\"video\":{\"id\":$(json_string "$VIDEO_ID")}}"
  http_retry "videos/edits" http_json POST "$V1/videos/edits" "$BODY_JSON" "" 60
  VID="$(json_field id "$BODY")"; [ -z "$VID" ] && { err "videos/edits response had no id"; exit 1; }
  wait_video "$VID"
  DEST="$OUT_DIR/minking-video-$STAMP.mp4"
  i=0
  while [ "$i" -lt 20 ]; do save_video "$VID" "$DEST" && { abs_path "$DEST"; exit 0; }; i=$((i+1)); sleep 3; done
  err "video content not ready"; exit 1
fi
