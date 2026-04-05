import io
import os
import json
import base64
import functions_framework
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.cloud import storage, secretmanager
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseDownload

load_dotenv(".env.local")

GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
GCS_BUCKET_NAME = os.environ["GCS_BUCKET_NAME"]

GDRIVE_NOTES_FOLDER_ID = os.environ["GDRIVE_NOTES_FOLDER_ID"]
GDRIVE_RECORDINGS_FOLDER_ID = os.environ["GDRIVE_RECORDINGS_FOLDER_ID"]
GDRIVE_PY_QUESTIONS_FOLDER_ID = os.environ.get("GDRIVE_PY_QUESTIONS_FOLDER_ID")
GDRIVE_PY_SNIPPETS_FOLDER_ID = os.environ.get("GDRIVE_PY_SNIPPETS_FOLDER_ID")

# Map each folder ID to its expected file extension (only set folders are included)
GDRIVE_FOLDERS = {
    folder_id: ext
    for folder_id, ext in [
        (GDRIVE_NOTES_FOLDER_ID, ".pdf"),
        (GDRIVE_RECORDINGS_FOLDER_ID, ".mp4"),
        (GDRIVE_PY_QUESTIONS_FOLDER_ID, ".py"),
        (GDRIVE_PY_SNIPPETS_FOLDER_ID, ".py"),
    ]
    if folder_id
}

SECRET_NAME = (
    f"projects/{GCP_PROJECT_ID}/secrets/drive-reader-oauth-creds/versions/latest"
)

# Chunk size for streaming: 10MB (must be a multiple of 256KB)
CHUNK_SIZE = 10 * 1024 * 1024


def get_drive_credentials():
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": SECRET_NAME})
    oauth_data = json.loads(response.payload.data.decode("utf-8"))

    creds = Credentials(
        token=None,
        refresh_token=oauth_data["refresh_token"],
        client_id=oauth_data["client_id"],
        client_secret=oauth_data["client_secret"],
        token_uri=oauth_data["token_uri"],
    )
    creds.refresh(Request())
    return creds


def get_drive_service(creds):
    return build("drive", "v3", credentials=creds)


def list_drive_files(service, folder_id):
    results = []
    page_token = None

    while True:
        # Get files whose parent folder is that provided, and is not in the bin (trashed=false),
        # returning only a limited set of metadata.
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, md5Checksum, mimeType, size)",
                pageToken=page_token,
            )
            .execute()
        )

        # Collect the returned file metadata
        results.extend(response.get("files", []))

        # Since the Drive API caps to 100 files per response, repeat request
        # until no more pages remain (i.e. nextPageToken is absent)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def get_gcs_metadata(bucket):
    checksums = {}
    for blob in bucket.list_blobs():
        if blob.md5_hash:
            # GCS returns MD5 as base64, GDrive gives hex - so we normalise to hex
            checksums[blob.name] = base64.b64decode(blob.md5_hash).hex()
    return checksums


def stream_drive_to_gcs(drive_service, file_meta, bucket):
    # Index the GDrive file's metadata
    file_id = file_meta["id"]
    file_name = file_meta["name"]
    mime_type = file_meta.get("mimeType", "application/octet-stream")

    # Request and download the file from GDrive
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=CHUNK_SIZE)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    # Create the downloaded file as a blob and upload it to GCS
    buffer.seek(0)
    blob = bucket.blob(file_name)
    blob.upload_from_file(
        buffer,
        content_type=mime_type,
    )


@functions_framework.http
def sync_drive_to_gcs(request):
    print("Starting Drive to GCS sync")

    try:
        creds = get_drive_credentials()
        drive_service = get_drive_service(creds)
        gcs_client = storage.Client()
        bucket = gcs_client.bucket(GCS_BUCKET_NAME)

        drive_files = []
        for folder_id in GDRIVE_FOLDERS:
            drive_files.extend(
                (file | {"_allowed_ext": GDRIVE_FOLDERS[folder_id]})
                for file in list_drive_files(drive_service, folder_id)
            )
        gcs_checksums = get_gcs_metadata(bucket)

        print(
            f"Found {len(drive_files)} file(s) in Drive, {len(gcs_checksums)} in GCS."
        )
        uploaded, skipped, errors = [], [], []

        for file in drive_files:
            name = file["name"]
            drive_md5 = file.get("md5Checksum")

            # Only sync files matching the folder's expected extension
            if os.path.splitext(name)[1].lower() != file["_allowed_ext"]:
                print(f"  Skipping (unsupported type): {name}")
                skipped.append({"file": name, "reason": "unsupported-type"})
                continue

            # Skip if MD5 matches, as the file is unchanged
            if drive_md5 and gcs_checksums.get(name) == drive_md5:
                print(f"  Unchanged, skipping: {name}")
                skipped.append({"file": name, "reason": "unchanged"})
                continue

            # Otherwise, stream to GCS from GDrive
            print(f"  Syncing: {name}")
            try:
                stream_drive_to_gcs(drive_service, file, bucket)
                uploaded.append(name)
                print(f"  Done: {name}")
            except Exception as e:
                print(f"  Error syncing {name}: {e}")
                errors.append({"file": name, "error": str(e)})

        result = {
            "uploaded": uploaded,
            "skipped": skipped,
            "errors": errors,
            "summary": f"{len(uploaded)} uploaded, {len(skipped)} skipped, {len(errors)} errors",
        }
        print(f"Sync complete: {result['summary']}")
        return (json.dumps(result), 200, {"Content-Type": "application/json"})

    except Exception as e:
        print(f"Fatal error: {e}")
        return (
            json.dumps({"error": str(e)}),
            500,
            {"Content-Type": "application/json"},
        )
