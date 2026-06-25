import boto3
from botocore.config import Config

from .config import settings

# signature_version='s3v4' is required for pre-signed GET URLs on KMS-encrypted objects.
# Without it, S3 returns: "InvalidArgument: Requests specifying Server Side Encryption
# with AWS KMS managed keys require AWS Signature Version 4."
_s3 = boto3.client(
    "s3",
    region_name=settings.aws_region,
    config=Config(signature_version="s3v4"),
)


def generate_presigned_get_url(object_key: str) -> str:
    """Presigned GET URL for a private report object, hard-capped to the configured TTL."""
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.reports_s3_bucket, "Key": object_key},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )


def put_object(object_key: str, data: bytes, content_type: str) -> None:
    """Upload report bytes to the private reports bucket."""
    _s3.put_object(
        Bucket=settings.reports_s3_bucket,
        Key=object_key,
        Body=data,
        ContentType=content_type,
    )
