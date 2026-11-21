#!/usr/bin/env bash
# One-time setup of the offsite bucket (S3 Glacier Deep Archive), run with admin credentials.
# Safe to rerun: each step is skipped or reapplied as-is.
#
#   scripts/offsite-setup.sh BUCKET [REGION]
#
# Default region us-west-2 (Oregon): Deep Archive costs the same as Ohio or Virginia, and uploads
# from the Bay Area ran ~3x faster there than to us-east-2, whose route went through a slow transit hop.
#
# - bucket with Object Lock (governance, 1-year default retention; turns on versioning)
# - all public access blocked
# - lifecycle: abort partial uploads after 7 days, prune old index/manifest versions after 400 days
# - IAM user photoark-uploader: list, upload, read, restore; no delete. Its keys go into the
#   AWS CLI profile "photoark", which photo-offsite uses by default.
set -euo pipefail

BUCKET=${1:?usage: scripts/offsite-setup.sh BUCKET [REGION]}
REGION=${2:-us-west-2}
USER_NAME=photoark-uploader
PROFILE=photoark

if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "bucket $BUCKET exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" --object-lock-enabled-for-bucket >/dev/null
  echo "created bucket $BUCKET in $REGION"
fi

aws s3api put-object-lock-configuration --bucket "$BUCKET" --region "$REGION" --object-lock-configuration \
  '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Years":1}}}'
aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" --lifecycle-configuration '{
  "Rules": [
    {"ID": "abort-partial-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
    {"ID": "prune-old-index", "Status": "Enabled", "Filter": {"Prefix": "index.tsv"},
     "NoncurrentVersionExpiration": {"NoncurrentDays": 400}},
    {"ID": "prune-old-manifests", "Status": "Enabled", "Filter": {"Prefix": "manifests/"},
     "NoncurrentVersionExpiration": {"NoncurrentDays": 400}}
  ]}' >/dev/null
echo "object lock, public access block and lifecycle set"

aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1 || aws iam create-user --user-name "$USER_NAME" >/dev/null
aws iam put-user-policy --user-name "$USER_NAME" --policy-name photoark-upload --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:ListBucket\", \"s3:ListBucketMultipartUploads\", \"s3:GetBucketLocation\"],
     \"Resource\": \"arn:aws:s3:::$BUCKET\"},
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:RestoreObject\",
                                        \"s3:AbortMultipartUpload\", \"s3:ListMultipartUploadParts\"],
     \"Resource\": \"arn:aws:s3:::$BUCKET/*\"}
  ]}"
echo "user $USER_NAME has upload-only access"

if aws configure get aws_access_key_id --profile "$PROFILE" >/dev/null 2>&1; then
  echo "profile $PROFILE already has keys"
else
  read -r key secret < <(aws iam create-access-key --user-name "$USER_NAME" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
  aws configure set aws_access_key_id "$key" --profile "$PROFILE"
  aws configure set aws_secret_access_key "$secret" --profile "$PROFILE"
  echo "new access key saved as AWS profile $PROFILE"
fi
aws configure set region "$REGION" --profile "$PROFILE"
# CRT streamed tars about twice as fast as the classic client, and only with small parts: each
# connection here is slow, so 64 MB parts ran 4x slower than 8 MB ones. CRT ignores --expected-size,
# so 10,000 parts x 8 MB caps a tar at ~78 GiB; photo-offsite keeps tars under 64 GiB (UNIT_MAX_BYTES).
# target_bandwidth bounds the memory CRT reserves for buffering parts.
aws configure set s3.preferred_transfer_client crt --profile "$PROFILE"
aws configure set s3.multipart_chunksize 8MB --profile "$PROFILE"
aws configure set s3.target_bandwidth 1Gb/s --profile "$PROFILE"
