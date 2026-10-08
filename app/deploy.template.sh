#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
: "${AWS_PROFILE:?Set AWS_PROFILE}"
: "${AWS_REGION:?Set AWS_REGION}"
: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID}"
: "${ECR_REPO:?Set ECR_REPO}"
: "${LAMBDA_NAME:?Set LAMBDA_NAME}"
export AWS_PROFILE AWS_REGION
IMAGE_TAG="${1:-v1}"
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform=linux/amd64 --provenance=false -t "$IMAGE_URI" .
docker push "$IMAGE_URI"
aws lambda update-function-code --function-name "$LAMBDA_NAME" --image-uri "$IMAGE_URI" --no-cli-pager
aws lambda wait function-updated --function-name "$LAMBDA_NAME"
