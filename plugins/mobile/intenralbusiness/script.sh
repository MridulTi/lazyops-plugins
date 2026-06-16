#!/bin/bash
set -e
set -o pipefail

# -----------------------------------------------------------------------------
# S3 email output:
#   - s3-download-links        — header + one presigned URL per line (legacy Groovy)
#   - s3-email-fragment.html   — <a href="...">Human label</a><br/>
#   - s3-download-links.tsv    — Label<TAB>URL per line
#
# Internal App Sharing (bundletool) email output:
#   - prod-release-url, staging-debug-url, … — https URLs only, one per line (all links; no logs)
#   - internal-app-sharing-fragment.html     — same as S3: named links for the mail body
#   - internal-app-sharing-links.tsv         — Label<TAB>URL
#
# Jenkins HTML body (example):
#   def s3Html = "ssh ... cat .../s3-email-fragment.html".execute().text.trim()
#   def iasHtml = "ssh ... cat .../internal-app-sharing-fragment.html".execute().text.trim()
#   builder.append("<b>Play Internal App Sharing</b><br/><br/>")
#   builder.append(iasHtml)
# -----------------------------------------------------------------------------
#
# Paths used by Jenkins email (Groovy): cat $OUT_DIR/<file>
OUT_DIR="$JENKINS_HOME/jenkins-builds/android-builds/$JOB_NAME/$BUILD_NUMBER"
# Gradle bundle outputs (AABs before rename/copy to OUT_DIR)
BUNDLE_BASE="${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs/bundle"

mkdir -p "$OUT_DIR"

# Shared for S3 and Internal App Sharing email fragments (href escaping).
html_escape_attr() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//\"/&quot;}"
  printf '%s' "$s"
}

# From arbitrary text (logs): extract only http(s) URLs, first occurrence order, de-duplicated.
# Trims trailing punctuation often glued to URLs in log lines.
extract_https_urls_ordered_unique() {
  grep -oE 'https?://[^[:space:]<>"]+' | sed 's/[)"\.,;:]+$//' | awk '!seen[$0]++'
}

cd ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs

#moving all builds in a folder

if [[ "$BUILD_VARIANT" == +(*"ProdRelease"*|*"StagingDebug"*|*"ProdDebug"*|*"EdcDebug"*|*"EdcRelease"*|*"All"*) ]]; then
find ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs/bundle/ -name "*.aab" -exec cp --recursive {} "$OUT_DIR"/ \;

find ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs/mapping/prodRelease/ -name "mapping.txt" -exec cp {} "$OUT_DIR"/ \;

fi

if [[ "$BUILD_VARIANT" == +(*"AppBundle"*|*"All"*) ]]; then
find ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs/bundle/prodRelease -name "*.aab" -exec cp {} "$OUT_DIR"/app.aab \;

find ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/app/build/outputs/mapping/prodRelease/ -name "mapping.txt" -exec cp {} "$OUT_DIR"/ \;

fi

#rename builds to have proper name

if [[ "$BUILD_VARIANT" == *"ProdRelease"* ]]; then
mv "$OUT_DIR"/app-prod-release.aab "$OUT_DIR"/BWP-prod-release-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"StagingDebug"* ]]; then
mv "$OUT_DIR"/app-staging-debug.aab "$OUT_DIR"/BWP-staging-debug-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"ProdDebug"* ]]; then
mv "$OUT_DIR"/app-prod-debug.aab "$OUT_DIR"/BWP-prod-debug-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"AppBundle"* ]]; then
mv "$OUT_DIR"/app.aab "$OUT_DIR"/BWP-prod-release-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"EdcDebug"* ]]; then
mv "$OUT_DIR"/app-edc-debug.aab "$OUT_DIR"/BWP-edc-debug-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"EdcRelease"* ]]; then
mv "$OUT_DIR"/app-edc-release.aab "$OUT_DIR"/BWP-edc-release-$VERSION"-"$RC.aab
fi

if [[ "$BUILD_VARIANT" == *"All"* ]]; then
mv "$OUT_DIR"/app-prod-release.aab "$OUT_DIR"/BWP-prod-release-$VERSION"-"$RC.aab
mv "$OUT_DIR"/app-staging-debug.aab "$OUT_DIR"/BWP-staging-debug-$VERSION"-"$RC.aab
mv "$OUT_DIR"/app-prod-debug.aab "$OUT_DIR"/BWP-prod-debug-$VERSION"-"$RC.aab
mv "$OUT_DIR"/app.aab "$OUT_DIR"/BWP-prod-release-$VERSION"-"$RC.aab
fi

# Upload to S3 (set S3_ANDROID_BUILDS_BUCKET in Jenkins job env, e.g. my-bucket)
# Optional: S3_ANDROID_BUILDS_REGION or AWS_REGION / AWS_DEFAULT_REGION (from AWS_REGION)
# Presigned GET lifetime: default ~2 months (60 days). Override with S3_PRESIGN_EXPIRES_SECONDS (seconds).
# Note: AWS S3 SigV4 presigned URLs (aws s3 presign) are limited to at most 604800 seconds (7 days).
# If presign fails with a long expiry, set S3_PRESIGN_EXPIRES_SECONDS=604800 or use CloudFront signed URLs for longer access.
#
# Why links die in minutes even when you set 7 days: presigned URLs are signed with whatever
# credentials are active. Temporary credentials (STS, AssumeRole, instance/SSO session, Jenkins
# with short-lived keys) expire independently — often in 15–60 minutes. The URL stops working
# when those credentials expire, NOT at X-Amz-Expires. Fix: presign with long-lived IAM user
# access keys (narrow S3 policy), or increase role max session duration, or use CloudFront
# signed URLs / a download proxy instead of raw S3 presign from ephemeral CI creds.

S3_BUCKET="${S3_ANDROID_BUILDS_BUCKET%/}"
S3_REGION="${S3_ANDROID_BUILDS_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
# 60 days ≈ 2 months
S3_PRESIGN_EXPIRES="${S3_PRESIGN_EXPIRES_SECONDS:-5184000}"

if [[ -n "$S3_BUCKET" ]]; then
  export AWS_DEFAULT_REGION="${S3_REGION}"
  if [[ "${S3_PRESIGN_EXPIRES:-0}" -gt 604800 ]]; then
    echo "$0: warning: AWS S3 presign (SigV4) usually allows at most 604800s (7 days); ${S3_PRESIGN_EXPIRES}s may fail — use S3_PRESIGN_EXPIRES_SECONDS=604800 or CloudFront for longer." >&2
  fi
  if [[ -n "${AWS_SESSION_TOKEN:-}" ]]; then
    echo "$0: warning: AWS_SESSION_TOKEN is set (temporary/STS credentials). Presigned URLs typically stop working when this session ends (often minutes–hours), regardless of --expires-in. For stable email links use IAM user keys for presign only, CloudFront, or a proxy." >&2
  fi

  shopt -s nullglob
  for f in "$OUT_DIR"/*.aab; do
    [[ -f "$f" ]] || continue
    aws s3 cp "$f" "s3://${S3_BUCKET}/bundle-p4b/$JOB_NAME/$BUILD_NUMBER/$(basename "$f")"
  done
  shopt -u nullglob
  if [[ -f "$OUT_DIR/mapping.txt" ]]; then
    aws s3 cp "$OUT_DIR/mapping.txt" "s3://${S3_BUCKET}/$JOB_NAME/$BUILD_NUMBER/mapping.txt"
  fi

  # Presigned GET URLs for email (works with private buckets; same creds as upload must allow s3:GetObject)
  # Human-readable labels (similar to main Android release mail): link text ≠ full URL.
  human_label_for_out_file() {
    local fname="$1"
    case "$fname" in
      *prod-release*.aab) printf '%s' "RELEASE BUILD (AAB) — production bundle" ;;
      *staging-debug*.aab) printf '%s' "STAGING BUILD (AAB)" ;;
      *prod-debug*.aab)    printf '%s' "PROD DEBUG BUILD (AAB)" ;;
      *edc-debug*.aab)     printf '%s' "EDC DEBUG BUILD (AAB)" ;;
      *edc-release*.aab)   printf '%s' "EDC RELEASE BUILD (AAB)" ;;
      mapping.txt)         printf '%s' "MAPPING — ProGuard mapping.txt" ;;
      *)                   printf '%s' "$fname" ;;
    esac
  }

  S3_FRAG="$OUT_DIR/s3-email-fragment.html"
  S3_TSV="$OUT_DIR/s3-download-links.tsv"
  : >"$S3_FRAG"
  : >"$S3_TSV"
  {
    echo "S3 presigned download URLs (expires in ${S3_PRESIGN_EXPIRES}s ≈ $((S3_PRESIGN_EXPIRES / 86400)) days):"
    for f in "$OUT_DIR"/*.aab; do
      [[ -f "$f" ]] || continue
      fname="$(basename "$f")"
      url=$(aws s3 presign "s3://${S3_BUCKET}/bundle-p4b/${JOB_NAME}/${BUILD_NUMBER}/${fname}" \
        --expires-in "${S3_PRESIGN_EXPIRES}" \
        --region "${S3_REGION}")
      echo "$url"
      label=$(human_label_for_out_file "$fname")
      printf '%s\t%s\n' "$label" "$url" >>"$S3_TSV"
      href=$(html_escape_attr "$url")
      printf '<a href="%s">%s</a><br/>\n' "$href" "$label" >>"$S3_FRAG"
    done
    if [[ -f "$OUT_DIR/mapping.txt" ]]; then
      url=$(aws s3 presign "s3://${S3_BUCKET}/${JOB_NAME}/${BUILD_NUMBER}/mapping.txt" \
        --expires-in "${S3_PRESIGN_EXPIRES}" \
        --region "${S3_REGION}")
      echo "$url"
      label=$(human_label_for_out_file "mapping.txt")
      printf '%s\t%s\n' "$label" "$url" >>"$S3_TSV"
      href=$(html_escape_attr "$url")
      printf '<a href="%s">%s</a><br/>\n' "$href" "$label" >>"$S3_FRAG"
    fi
  } >"$OUT_DIR/s3-download-links"
fi

cd ${JENKINS_HOME}/workspace/${JOB_NAME}/${BUILD_NUMBER}/bundletool
/usr/local/python3.8-openssl111/bin/python3.8 -m venv .venv
source .venv/bin/activate
pip install -r requirements-upload.txt

# Internal App Sharing — full log to console; OUT_DIR/<url_file> = URLs only (one per line, no logs).
# Multi-select BUILD_VARIANT: use independent checks (not elif) so every selected variant uploads.
upload_internal_app_sharing_aab() {
  local aab_path="$1"
  local url_file="$2"
  if [[ -f "$aab_path" ]]; then
    local tmp
    tmp=$(mktemp)
    python upload_internal_app_sharing.py \
      ${GPLAY_SERVICE_ACCOUNT:?Set GPLAY_SERVICE_ACCOUNT} \
      ${GPLAY_PACKAGE_NAME:?Set GPLAY_PACKAGE_NAME} \
      "$aab_path" \
      ${GPLAY_KEY_FILE:?Set GPLAY_KEY_FILE} \
      2>&1 | tee "$tmp"
    extract_https_urls_ordered_unique <"$tmp" >"$OUT_DIR/$url_file"
    rm -f "$tmp"
    if [[ ! -s "$OUT_DIR/$url_file" ]]; then
      rm -f "$OUT_DIR/$url_file"
      echo "$0: warning: no https URL in upload output for $url_file (AAB: $aab_path)" >&2
    fi
  fi
}

if [[ "$BUILD_VARIANT" == *"All"* ]]; then
  upload_internal_app_sharing_aab "${BUNDLE_BASE}/prodRelease/app-prod-release.aab" "prod-release-url"
  upload_internal_app_sharing_aab "${BUNDLE_BASE}/stagingDebug/app-staging-debug.aab" "staging-debug-url"
  upload_internal_app_sharing_aab "${BUNDLE_BASE}/prodDebug/app-prod-debug.aab" "prod-debug-url"
  upload_internal_app_sharing_aab "${BUNDLE_BASE}/edcDebug/app-edc-debug.aab" "edc-debug-url"
  upload_internal_app_sharing_aab "${BUNDLE_BASE}/edcRelease/app-edc-release.aab" "edc-release-url"
else
  [[ "$BUILD_VARIANT" == *"ProdRelease"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/prodRelease/app-prod-release.aab" "prod-release-url"
  [[ "$BUILD_VARIANT" == *"StagingDebug"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/stagingDebug/app-staging-debug.aab" "staging-debug-url"
  [[ "$BUILD_VARIANT" == *"ProdDebug"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/prodDebug/app-prod-debug.aab" "prod-debug-url"
  [[ "$BUILD_VARIANT" == *"EdcDebug"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/edcDebug/app-edc-debug.aab" "edc-debug-url"
  [[ "$BUILD_VARIANT" == *"EdcRelease"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/edcRelease/app-edc-release.aab" "edc-release-url"
  [[ "$BUILD_VARIANT" == *"AppBundle"* ]] && \
    upload_internal_app_sharing_aab "${BUNDLE_BASE}/prodRelease/app-prod-release.aab" "app-bundle-url"
fi

# "All" builds: email template still has an app-bundle row — reuse prod release internal-sharing output.
if [[ "$BUILD_VARIANT" == *"All"* ]] && [[ -f "$OUT_DIR/prod-release-url" ]]; then
  cp "$OUT_DIR/prod-release-url" "$OUT_DIR/app-bundle-url"
fi

# HTML fragment: link text = readable name, href = URL (for Jenkins: cat internal-app-sharing-fragment.html)
# Also writes internal-app-sharing-links.tsv (Label<TAB>URL).
write_internal_app_sharing_email_fragment() {
  local frag="$OUT_DIR/internal-app-sharing-fragment.html"
  local tsv="$OUT_DIR/internal-app-sharing-links.tsv"
  : >"$frag"
  : >"$tsv"
  local f lbl
  while IFS='|' read -r f lbl; do
    [[ -z "$f" ]] && continue
    f="${f%"${f##*[![:space:]]}"}"
    f="${f#"${f%%[![:space:]]*}"}"
    lbl="${lbl%"${lbl##*[![:space:]]}"}"
    [[ -f "$OUT_DIR/$f" ]] || continue
    local -a urls=()
    local u
    while IFS= read -r u; do
      [[ -n "$u" ]] && urls+=("$u")
    done < <(extract_https_urls_ordered_unique <"$OUT_DIR/$f")
    [[ ${#urls[@]} -eq 0 ]] && continue
    local idx nu disp href
    nu=${#urls[@]}
    idx=0
    for url in "${urls[@]}"; do
      idx=$((idx + 1))
      disp="$lbl"
      [[ "$nu" -gt 1 ]] && disp="$lbl ($idx)"
      printf '%s\t%s\n' "$disp" "$url" >>"$tsv"
      href=$(html_escape_attr "$url")
      printf '<a href="%s">%s</a><br/>\n' "$href" "$disp" >>"$frag"
    done
  done <<'IAS_ORDER'
prod-release-url|INSTALL PROD RELEASE
staging-debug-url|INSTALL STAGING
prod-debug-url|INSTALL PROD DEBUG
app-bundle-url|INSTALL APP BUNDLE (PROD RELEASE)
edc-debug-url|INSTALL EDC DEBUG
edc-release-url|INSTALL EDC RELEASE
IAS_ORDER
}

write_internal_app_sharing_email_fragment
