# Third-party notices

Paymenter Backups is copyright (c) 2026 Autonomic Engineering and is released
under the MIT license. Paymenter itself is a separate MIT
project and is never bundled or modified by this extension. The native adapter
uses Paymenter's installed Laravel and Filament APIs without changing its
Composer dependencies.

Release builds include isolated Python SDK distributions pinned with wheel
SHA256 hashes in `requirements-s3.lock`. `SDK-MANIFEST.json` lists the exact
bundled distributions and lock digest. Their original license and notice files
are copied into `Backups/THIRD_PARTY_LICENSES/`, and the wheel metadata remains
in `Backups/runtime/sdk/`. Boto3 and Botocore use Apache-2.0; s3transfer uses
Apache-2.0; jmespath uses MIT; python-dateutil uses Apache-2.0/BSD; six uses MIT;
urllib3 uses MIT. Consult the shipped original notices for attribution and terms.

Only the S3 botocore service model directory and shared endpoint/partition data
are retained; other AWS service model directories are deterministically removed
to fit the native 10 MiB uploader limit. This SDK bundle supports S3 only. Wheel
RECORD metadata describes the original wheel, and SDK-MANIFEST documents pruning.
No application dependency is installed during production extension deployment.
